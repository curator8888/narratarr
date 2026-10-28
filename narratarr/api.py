"""HTTP API + static web UI."""

from __future__ import annotations

import asyncio
import base64
import hmac
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brave, config, db, media, search
from .realdebrid import RealDebrid, RealDebridError
from .sources import ORDER, all_sources, get_source, source_for_url
from .worker import Worker

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    worker = Worker()
    app.state.worker = worker
    worker.start()
    yield
    await worker.stop()


app = FastAPI(title="Narratarr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    # Make browsers re-check UI files so updates show up without a hard refresh.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def require_password(request, call_next):
    """Optional login (any username + $NARRATARR_PASSWORD), for when the app is reachable from a network."""
    password = os.environ.get("NARRATARR_PASSWORD", "")
    if not password or request.url.path == "/api/health":
        return await call_next(request)
    given = ""
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            given = base64.b64decode(encoded).decode("utf-8").partition(":")[2]
        except (ValueError, UnicodeDecodeError):
            pass
    if hmac.compare_digest(given.encode(), password.encode()):
        return await call_next(request)
    return Response(
        "Narratarr needs a password.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Narratarr"'}
    )


def _worker() -> Worker:
    return app.state.worker


def _source_error(e: Exception, label: str = "AudiobookBay") -> HTTPException:
    if isinstance(e, httpx.HTTPStatusError):
        return HTTPException(502, f"{label} returned an error ({e.response.status_code}). It may be down or moved.")
    return HTTPException(
        502, f"Couldn't reach {label}. The site may be down. Try again in a few minutes, or try another source."
    )


def _resolve(url: str, name: str | None = None):
    """The source a book link belongs to (the named one, if given)."""
    try:
        source = get_source(name) if name else source_for_url(url)
    except ValueError:
        raise HTTPException(400, "Unknown source.")
    if source is None or not source.owns(url):
        raise HTTPException(400, "That link isn't from a supported source.")
    return source


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"ok": True}


# ---- discover -------------------------------------------------------------


@app.get("/api/sources")
async def sources():
    found = all_sources()
    return [
        {
            "name": name,
            "label": found[name].label,
            "description": found[name].description,
            "delivery": found[name].delivery,
            "default_label": found[name].default_label,
            "categories": [{"slug": c.slug, "name": c.name} for c in found[name].categories()],
        }
        for name in ORDER
    ]


@app.get("/api/discover")
async def discover(q: str = "", category: str = "", page: int = 1, source: str = "librivox"):
    try:
        src = get_source(source)
    except ValueError:
        raise HTTPException(400, "Unknown source.")
    settings = config.load()
    use_brave = src.name == "audiobookbay" and settings.search_provider == "brave" and settings.brave_api_key
    try:
        if q.strip() and use_brave:
            data = await search.brave_smart_search(
                q.strip(), settings.brave_api_key, settings.abb_base_url, page, settings.smart_search
            )
        elif q.strip():
            data = await search.smart_search(src, q.strip(), page, use_suggestions=settings.smart_search)
        else:
            data = (await src.latest(page, category or None)).to_dict()
    except httpx.HTTPError as e:
        raise _source_error(e, src.label)
    except brave.BraveError as e:
        raise HTTPException(502, str(e))
    owned = {b["source_url"]: b for b in db.list_books()}
    for item in data["items"]:
        b = owned.get(item["url"])
        item["source"] = src.name
        item["library"] = {"id": b["id"], "status": b["status"]} if b else None
    return data


@app.get("/api/suggest")
async def suggest(q: str):
    if len(q.strip()) < 3 or not config.load().smart_search:
        return []
    return search.relevant(q, await search.suggestions(q.strip(), limit=8))[:6]


@app.get("/api/details")
async def details(url: str, source: str | None = None):
    src = _resolve(url, source)
    try:
        d = await src.details(url)
    except httpx.HTTPError as e:
        raise _source_error(e, src.label)
    data = d.to_dict()
    data["source"] = src.name
    b = db.get_by_url(url)
    data["library"] = {"id": b["id"], "status": b["status"]} if b else None
    return data


# ---- library --------------------------------------------------------------


class AddBook(BaseModel):
    url: str
    download: bool = True
    source: str | None = None


@app.get("/api/library")
async def library():
    return db.list_books()


@app.get("/api/stats")
async def stats():
    return db.stats()


@app.post("/api/library")
async def add_book(body: AddBook):
    source = _resolve(body.url, body.source)
    existing = db.get_by_url(body.url)
    if existing:
        if body.download and existing["status"] in ("wanted", "failed"):
            db.update(existing["id"], status="queued", error="")
            _worker().poke()
        return db.get(existing["id"])
    try:
        d = await source.details(body.url)
    except httpx.HTTPError as e:
        raise _source_error(e, source.label)
    book = db.insert(
        {
            "source": source.name,
            "source_url": d.url,
            "title": d.title,
            "author": d.author,
            "narrators": d.narrators,
            "cover_url": d.cover_url,
            "description": d.description,
            "language": d.language,
            "categories": d.categories,
            "format": d.format,
            "bitrate": d.bitrate,
            "length": d.length,
            "size": d.size,
            "info_hash": d.info_hash,
            "trackers": d.trackers,
            "files": d.files,
            "status": "queued" if body.download else "wanted",
        }
    )
    if body.download:
        _worker().poke()
    return book


def _get_or_404(book_id: int) -> dict:
    book = db.get(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return book


@app.post("/api/library/{book_id}/download")
async def download(book_id: int):
    book = _get_or_404(book_id)
    if book["status"] in db.ACTIVE:
        return book
    db.update(book_id, status="queued", error="", progress=0, detail="")
    _worker().poke()
    return db.get(book_id)


@app.post("/api/library/{book_id}/stop")
async def stop(book_id: int):
    _get_or_404(book_id)
    _worker().cancel(book_id)
    db.update(book_id, status="wanted", speed=0, progress=0, detail="")
    return db.get(book_id)


@app.delete("/api/library/{book_id}")
async def remove(book_id: int, delete_files: bool = False):
    book = _get_or_404(book_id)
    _worker().cancel(book_id)
    if delete_files and book["path"]:
        root = Path(config.load().library_dir).expanduser().resolve()
        path = Path(book["path"]).resolve()
        if root in path.parents and path.exists():
            shutil.rmtree(path, ignore_errors=True)
    db.delete(book_id)
    return {"ok": True}


# ---- player ---------------------------------------------------------------


def _book_dir(book_id: int) -> Path:
    book = _get_or_404(book_id)
    path = Path(book["path"]) if book["path"] else None
    if book["status"] != "completed" or not path or not path.is_dir():
        raise HTTPException(404, "This book hasn't been downloaded yet.")
    return path


@app.get("/api/library/{book_id}/media")
async def book_media(book_id: int):
    info = await asyncio.to_thread(media.describe, _book_dir(book_id))
    if not info["files"]:
        raise HTTPException(404, "No playable audio files were found in this book's folder.")
    return info


@app.get("/api/library/{book_id}/stream/{index}")
async def stream(book_id: int, index: int):
    book_dir = _book_dir(book_id)
    files = await asyncio.to_thread(media.audio_files, book_dir)
    if not 0 <= index < len(files):
        raise HTTPException(404, "No such file")
    path = files[index]
    # FileResponse handles Range requests, so the player can seek instantly.
    return FileResponse(path, media_type=media.AUDIO_TYPES[path.suffix.lower()])


@app.get("/api/library/{book_id}/cover")
async def cover(book_id: int):
    book = _get_or_404(book_id)
    if book["path"]:
        for found in sorted(Path(book["path"]).glob("cover.*")):
            return FileResponse(found)
    if book["cover_url"].startswith(("http://", "https://")):
        return RedirectResponse(book["cover_url"])
    raise HTTPException(404, "No cover")


class Progress(BaseModel):
    position: float
    duration: float = 0
    finished: bool | None = None


@app.put("/api/library/{book_id}/progress")
async def save_progress(book_id: int, body: Progress):
    _get_or_404(book_id)
    changes = {"listen_position": max(0.0, body.position), "listened_at": time.time()}
    if body.duration:
        changes["listen_duration"] = body.duration
    if body.finished is not None:
        changes["finished"] = int(body.finished)
        if body.finished:
            changes["listen_position"] = 0.0
    db.update(book_id, **changes)
    return {"ok": True}


# ---- settings -------------------------------------------------------------


@app.get("/api/settings")
async def get_settings():
    return config.load().public()


@app.put("/api/settings")
async def put_settings(changes: dict):
    return config.update(changes).public()


@app.post("/api/settings/test-brave")
async def test_brave():
    s = config.load()
    try:
        res = await brave.search("dune", s.brave_api_key, s.abb_base_url)
    except brave.BraveError as e:
        raise HTTPException(400, str(e))
    return {"results": len(res["items"])}


@app.post("/api/settings/test-rd")
async def test_rd():
    try:
        async with RealDebrid(config.load().rd_api_key) as rd:
            user = await rd.user()
    except RealDebridError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach Real-Debrid.")
    return {
        "username": user.get("username"),
        "type": user.get("type"),
        "expiration": user.get("expiration"),
        "points": user.get("points"),
    }
