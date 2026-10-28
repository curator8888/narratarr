"""Background download pipeline.

For each queued book:
  resolving   - make sure we have the info hash (scrape the book page if needed)
  caching     - hand the magnet to Real-Debrid and wait for it to finish on their side
  downloading - unrestrict each link and stream the files to a temp folder
  organizing  - unzip, move into the library, add cover/description, notify Audiobookshelf
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path

import httpx

from . import config, db
from .realdebrid import DEAD, RealDebrid, RealDebridError
from .sources import Details, get_source

log = logging.getLogger("narratarr.worker")

POLL_SECONDS = 4
RD_TIMEOUT_SECONDS = 6 * 60 * 60
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(part: str, fallback: str = "Unknown") -> str:
    part = _UNSAFE.sub("", part).strip().strip(".")
    return " ".join(part.split())[:150] or fallback


def destination(book: dict, settings: config.Settings) -> Path:
    root = Path(settings.library_dir).expanduser().resolve()
    values = {
        "author": safe_name(book["author"], "Unknown Author"),
        "title": safe_name(book["title"], "Untitled"),
        "narrator": safe_name(", ".join(book["narrators"][:2]), "Unknown Narrator"),
    }
    template = settings.folder_template or "{author}/{title}"
    try:
        parts = [safe_name(p.format(**values)) for p in template.split("/") if p.strip()]
    except (KeyError, IndexError, ValueError):
        parts = [values["author"], values["title"]]
    dest = root.joinpath(*parts).resolve()
    if root not in dest.parents:
        raise ValueError("Folder template points outside the library folder")
    return dest


def extract_zip(archive: Path, into: Path) -> None:
    into = into.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (into / member.filename).resolve()
            if into != target and into not in target.parents:
                raise ValueError(f"Unsafe path in archive: {member.filename}")
        zf.extractall(into)
    archive.unlink()


class Worker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # transport is injectable so tests can fake Real-Debrid and file hosts.
        self.running: dict[int, asyncio.Task] = {}
        self._transport = transport
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ---- scheduling -------------------------------------------------------

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self.running.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def poke(self) -> None:
        self._wake.set()

    def cancel(self, book_id: int) -> bool:
        task = self.running.get(book_id)
        if task:
            task.cancel()
            return True
        return False

    async def _loop(self) -> None:
        while True:
            try:
                limit = config.load().max_concurrent
                while len(self.running) < limit:
                    book = db.next_queued(set(self.running))
                    if not book:
                        break
                    self._spawn(book)
            except Exception:
                log.exception("Scheduler error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3)
            except TimeoutError:
                pass

    def _spawn(self, book: dict) -> None:
        book_id = book["id"]
        db.update(book_id, status="resolving", error="", detail="Starting…", progress=0)
        task = asyncio.create_task(self._run(book_id))
        self.running[book_id] = task
        task.add_done_callback(lambda _: (self.running.pop(book_id, None), self.poke()))

    async def _run(self, book_id: int) -> None:
        try:
            await self.process(book_id)
        except asyncio.CancelledError:
            raise
        except (RealDebridError, httpx.HTTPError, ValueError, OSError, zipfile.BadZipFile) as e:
            log.warning("Book %s failed: %s", book_id, e)
            db.update(book_id, status="failed", error=str(e) or type(e).__name__, speed=0, detail="")
        except Exception as e:
            log.exception("Book %s crashed", book_id)
            db.update(book_id, status="failed", error=f"Unexpected error: {e}", speed=0, detail="")

    # ---- pipeline ---------------------------------------------------------

    async def process(self, book_id: int) -> None:
        book = db.get(book_id)
        if book is None:
            return
        source = get_source(book["source"])
        if source.delivery == "direct":
            await self._process_direct(book, source)
        else:
            await self._process_torrent(book, source)

    def _fresh_tmp(self, book_id: int, settings: config.Settings) -> Path:
        tmp = Path(settings.library_dir).expanduser() / ".narratarr-tmp" / str(book_id)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        return tmp

    async def _finish(self, book: dict, tmp: Path, settings: config.Settings, **extra) -> None:
        db.update(book["id"], status="organizing", progress=100, speed=0, detail="Organizing files…")
        dest = await self._organize(book, tmp, settings)
        db.update(
            book["id"], status="completed", path=str(dest), progress=100, speed=0, detail="",
            completed_at=time.time(), **extra,
        )
        await self._notify_audiobookshelf(settings)

    async def _process_direct(self, book: dict, source) -> None:
        """Free sources (LibriVox, Old-Time Radio): plain HTTPS downloads, no Real-Debrid."""
        settings = config.load()
        book_id = book["id"]
        if not book["files"]:
            db.update(book_id, detail="Reading book page…")
            details = await source.details(book["source_url"])
            if not details.files:
                raise ValueError("This item has no downloadable audio files.")
            db.update(book_id, files=details.files)
            book = db.get(book_id)

        db.update(book_id, status="downloading", progress=0, detail="Starting download…")
        files = [
            # Keep sub-folder names in the file name so same-named files don't collide.
            {"filename": f["name"].replace("/", " - "), "filesize": f.get("size", 0), "download": f["url"]}
            for f in book["files"]
        ]
        tmp = self._fresh_tmp(book_id, settings)
        await self._download(book_id, files, tmp)
        await self._finish(book, tmp, settings)

    async def _process_torrent(self, book: dict, source) -> None:
        settings = config.load()
        book_id = book["id"]
        if not book["info_hash"]:
            db.update(book_id, detail="Reading book page…")
            details = await source.details(book["source_url"])
            if not details.info_hash:
                raise ValueError("This listing doesn't show an info hash.")
            db.update(book_id, info_hash=details.info_hash, trackers=details.trackers)
            book = db.get(book_id)

        magnet = Details(
            url=book["source_url"], title=book["title"], info_hash=book["info_hash"], trackers=book["trackers"]
        ).magnet()

        async with RealDebrid(settings.rd_api_key, transport=self._transport) as rd:
            torrent_id = book["rd_torrent_id"]
            if torrent_id:
                try:
                    await rd.torrent_info(torrent_id)
                except RealDebridError:
                    torrent_id = ""
            if not torrent_id:
                torrent_id = await rd.add_magnet(magnet)
                db.update(book_id, rd_torrent_id=torrent_id)

            db.update(book_id, status="caching", detail="Sent to Real-Debrid")
            info = await self._wait_for_rd(rd, book_id, torrent_id)

            links = info.get("links") or []
            if not links:
                raise RealDebridError("Real-Debrid finished but returned no files.")

            db.update(book_id, status="downloading", progress=0, detail="Preparing download…")
            direct = [await rd.unrestrict(link) for link in links]
            tmp = self._fresh_tmp(book_id, settings)
            await self._download(book_id, direct, tmp)

            if settings.delete_from_rd_after:
                try:
                    await rd.delete_torrent(torrent_id)
                except (RealDebridError, httpx.HTTPError):
                    log.info("Couldn't remove torrent %s from Real-Debrid", torrent_id)

        await self._finish(book, tmp, settings, rd_torrent_id="" if settings.delete_from_rd_after else torrent_id)

    async def _wait_for_rd(self, rd: RealDebrid, book_id: int, torrent_id: str) -> dict:
        started = time.monotonic()
        while True:
            info = await rd.torrent_info(torrent_id)
            status = info.get("status", "")
            if status == "downloaded":
                return info
            if status in DEAD:
                raise RealDebridError(f"Real-Debrid couldn't fetch this torrent ({status}).")
            if status == "waiting_files_selection":
                await rd.select_all_files(torrent_id)
            label = {
                "magnet_conversion": "Real-Debrid is reading the magnet…",
                "waiting_files_selection": "Selecting files…",
                "queued": "Queued on Real-Debrid",
                "downloading": f"Real-Debrid is caching · {info.get('seeders') or 0} seeders",
                "compressing": "Real-Debrid is packaging files…",
                "uploading": "Real-Debrid is finishing up…",
            }.get(status, f"Real-Debrid: {status}")
            db.update(book_id, progress=float(info.get("progress") or 0), speed=int(info.get("speed") or 0), detail=label)
            if time.monotonic() - started > RD_TIMEOUT_SECONDS:
                raise RealDebridError("Timed out waiting for Real-Debrid. The torrent may have no seeders.")
            await asyncio.sleep(POLL_SECONDS)

    async def _download(self, book_id: int, files: list[dict], tmp: Path) -> None:
        total = sum(int(f.get("filesize") or 0) for f in files) or 1
        done = 0
        last_report, window_start, window_bytes = 0.0, time.monotonic(), 0
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(60, read=120), transport=self._transport
        ) as client:
            for i, f in enumerate(files, 1):
                name = safe_name(f.get("filename") or f"part-{i}")
                async with client.stream("GET", f["download"]) as resp:
                    resp.raise_for_status()
                    with open(tmp / name, "wb") as out:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            out.write(chunk)
                            done += len(chunk)
                            window_bytes += len(chunk)
                            now = time.monotonic()
                            if now - last_report >= 0.75:
                                speed = int(window_bytes / max(now - window_start, 1e-6))
                                window_start, window_bytes, last_report = now, 0, now
                                db.update(
                                    book_id,
                                    progress=min(100.0, done * 100 / total),
                                    speed=speed,
                                    detail=f"Downloading {i} of {len(files)} · {name}",
                                )

    async def _organize(self, book: dict, tmp: Path, settings: config.Settings) -> Path:
        for archive in list(tmp.glob("*.zip")):
            await asyncio.to_thread(extract_zip, archive, tmp)

        dest = destination(book, settings)
        dest.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            await asyncio.to_thread(shutil.move, str(item), str(target))
        shutil.rmtree(tmp, ignore_errors=True)

        # Audiobookshelf picks these up automatically.
        if book["description"]:
            (dest / "desc.txt").write_text(book["description"])
        if book["narrators"]:
            (dest / "reader.txt").write_text(", ".join(book["narrators"]))
        if book["cover_url"] and not any(dest.glob("cover.*")):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=20, transport=self._transport) as client:
                    resp = await client.get(book["cover_url"])
                if resp.is_success and resp.headers.get("content-type", "").startswith("image/"):
                    ext = {"image/png": "png", "image/webp": "webp"}.get(resp.headers["content-type"].split(";")[0], "jpg")
                    (dest / f"cover.{ext}").write_bytes(resp.content)
            except httpx.HTTPError:
                pass
        return dest

    async def _notify_audiobookshelf(self, settings: config.Settings) -> None:
        if not (settings.audiobookshelf_url and settings.audiobookshelf_token and settings.audiobookshelf_library_id):
            return
        url = f"{settings.audiobookshelf_url.rstrip('/')}/api/libraries/{settings.audiobookshelf_library_id}/scan"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(url, headers={"Authorization": f"Bearer {settings.audiobookshelf_token}"})
        except httpx.HTTPError:
            log.info("Audiobookshelf scan request failed")
