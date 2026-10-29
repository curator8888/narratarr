"""Wanted books: finding releases for them, picking one automatically, and following authors and series.

A Wanted book that came from Audible's catalog has no release yet. Narratarr
searches Prowlarr (and LibriVox, for public-domain titles) for it, scores each
result against the book's title and author, and grabs the best one that a
configured download client can handle. Followed authors and series add their
new books to Wanted on their own.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date

from rapidfuzz import fuzz

from . import audible, config, db, downloaders
from .search import normalize
from .sources import ProwlarrError, get_source

log = logging.getLogger("narratarr.wanted")

AUTO_MATCH = 85  # how sure we must be before grabbing a release on our own
SEARCHES_PER_ROUND = 3  # be gentle with indexer API limits
FOLLOW_REFRESH_SECONDS = 12 * 3600


def main_title(title: str) -> str:
    """'The Final Empire: Mistborn, Book 1' -> 'The Final Empire'."""
    return re.split(r"\s*[:(\[]|\s+-\s+", title, maxsplit=1)[0].strip() or title


def first_author(author: str) -> str:
    return re.split(r",|&| and ", author or "", maxsplit=1)[0].strip()


def match(book: dict, item: dict) -> int:
    """0-100: how sure we are that a search result is this book."""
    title = normalize(main_title(book["title"]))
    author = normalize(first_author(book["author"]))
    if item.get("author"):
        t = fuzz.token_set_ratio(title, normalize(main_title(item["title"])))
        a = fuzz.token_set_ratio(author, normalize(item["author"])) if author else 50
    else:  # Prowlarr releases are just a name: "Author - Title (Year) [M4B]"
        text = normalize(item.get("release") or item.get("title") or "")
        t = fuzz.partial_ratio(title, text) if len(title) >= 4 else fuzz.token_set_ratio(title, text)
        surname = author.split()[-1] if author else ""
        a = 100 if surname and surname in text.split() else fuzz.partial_ratio(author, text) if author else 50
    if t < 70:
        return round(t * 0.6)
    score = round(0.7 * t + 0.3 * a)
    return min(score, 75) if author and a < 75 else score  # the right title by someone else isn't a match


def usable(item: dict, s: config.Settings) -> bool:
    """Whether a configured download client can fetch this result."""
    if item.get("source") in ("librivox", "oldtimeradio"):
        return True
    if item.get("protocol") == "usenet":
        return downloaders.usenet_configured(s)
    return downloaders.torrent_configured(s)


def _preference(item: dict) -> tuple:
    free = item.get("source") in ("librivox", "oldtimeradio")
    m4b = (item.get("format") or "").upper() == "M4B"
    seeders = item.get("seeders")
    alive = 1 if seeders is None or seeders > 0 else 0
    return (item["match"] >= 95, alive, free, m4b, seeders if seeders is not None else 50)


async def find_releases(book: dict) -> list[dict]:
    """Search everywhere a book could come from; best candidates first."""
    s = config.load()
    title, author = main_title(book["title"]), first_author(book["author"])
    query = f"{title} {author}".strip()
    jobs = {"librivox": get_source("librivox").find(title, author)}
    if s.prowlarr_url and s.prowlarr_api_key:
        jobs["prowlarr"] = get_source("prowlarr").search(query)
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    found, errors = [], []
    for name, res in zip(jobs, results):
        if isinstance(res, BaseException):
            errors.append(str(res) if isinstance(res, ProwlarrError) else f"Couldn't search {name}.")
            continue
        for listing in res.items:
            item = {**listing.to_dict(), "source": name}
            item["match"] = match(book, item)
            item["usable"] = usable(item, s)
            if item["match"] >= 60:
                found.append(item)
    if not found and errors:
        raise ProwlarrError(errors[0])
    found.sort(key=lambda i: (i["usable"], *_preference(i)), reverse=True)
    return found


def best(found: list[dict]) -> dict | None:
    for item in found:
        if item["usable"] and item["match"] >= AUTO_MATCH and item.get("seeders") != 0:
            return item
    return None


async def grab(book_id: int, url: str, source_name: str) -> dict:
    """Attach a release to a Wanted book and queue it, keeping the book's own metadata."""
    book = db.get(book_id)
    src = get_source(source_name)
    d = await src.details(url)
    db.update(
        book_id,
        source=src.name, release_url=url, protocol=d.protocol, download_url=d.download_url, info_hash=d.info_hash,
        trackers=d.trackers, files=d.files, format=d.format or book["format"], bitrate=d.bitrate,
        size=d.size or book["size"], indexer=d.indexer, release=d.release or d.title,
        description=book["description"] or d.description, cover_url=book["cover_url"] or d.cover_url,
        status="queued", error="", detail="", progress=0, client_job_id="", download_client="", rd_torrent_id="",
        search_note="",
    )
    return db.get(book_id)


async def search_and_grab(book: dict) -> dict | None:
    """Look for a Wanted book once. Returns the chosen release, or None if nothing good turned up."""
    db.update(book["id"], last_searched=time.time())
    try:
        found = await find_releases(book)
    except ProwlarrError as e:
        db.update(book["id"], search_note=str(e))
        raise
    pick = best(found)
    if not pick:
        close = [f for f in found if f["match"] >= AUTO_MATCH]
        note = (
            "Found matches, but none your download clients can fetch." if close
            else f"Nothing that matches yet ({len(found)} near misses)." if found else "Nothing found yet."
        )
        db.update(book["id"], search_note=note)
        return None
    await grab(book["id"], pick["url"], pick["source"])
    return pick


def _released(b: dict) -> bool:
    try:
        return date.fromisoformat(b.get("released") or "1900-01-01") <= date.today()
    except ValueError:
        return True


def due_for_search() -> list[dict]:
    s = config.load()
    cutoff = time.time() - s.auto_search_hours * 3600
    due = [
        b for b in db.list_books()
        if b["status"] == "wanted" and b["source"] == "audible" and _released(b)
        and (b["last_searched"] or 0) < cutoff
    ]
    due.sort(key=lambda b: b["last_searched"] or 0)
    return due[:SEARCHES_PER_ROUND]


async def auto_search() -> int:
    """One round of automatic searching. Returns how many books were grabbed."""
    s = config.load()
    if not (s.auto_search and s.prowlarr_url and s.prowlarr_api_key):
        return 0
    grabbed = 0
    for b in due_for_search():
        try:
            if await search_and_grab(b):
                grabbed += 1
        except Exception as e:  # one bad search shouldn't stop the round
            log.info("Automatic search for %s failed: %s", b["title"], e)
    return grabbed


# ---- library helpers ----------------------------------------------------------


def _key(title: str, author: str) -> tuple[str, str]:
    return normalize(main_title(title)), normalize(first_author(author))


def in_library(asin: str, title: str, author: str, books: list[dict] | None = None) -> dict | None:
    key = _key(title, author)
    for b in books if books is not None else db.list_books():
        if (asin and b.get("asin") == asin) or _key(b["title"], b["author"]) == key:
            return b
    return None


def add_wanted(b: dict) -> dict | None:
    """Add an Audible book to Wanted, unless it's already in the library in any form."""
    if in_library(b["asin"], b["title"], b["author"]) or db.get_by_url(b["url"]):
        return None
    return db.insert({
        "source": "audible", "source_url": b["url"], "asin": b["asin"], "title": b["title"], "author": b["author"],
        "narrators": b["narrators"], "cover_url": b["cover_url"], "description": b["description"],
        "length": b["length"], "language": b["language"], "series": b["series"], "released": b["released"],
        "status": "wanted",
    })


# ---- following ----------------------------------------------------------------


async def _books_for(kind: str, asin: str, name: str = "") -> tuple[str, str, list[dict]]:
    if kind == "author":
        info = await audible.author(asin)
        name = info["name"] or name
        return name, info["image"], await audible.author_books(asin, name)
    title, books = await audible.series_books(asin)
    return title or name, (books[0]["cover_url"] if books else ""), books


async def follow(kind: str, asin: str, name: str = "", add_existing: bool = False) -> tuple[dict, int]:
    """Start following. Upcoming books always go to Wanted; existing ones only if asked."""
    name, image, books = await _books_for(kind, asin, name)
    f = db.add_follow(kind, asin, name, image, [b["asin"] for b in books])
    added = sum(1 for b in books if (add_existing or not _released(b)) and add_wanted(b))
    return f, added


async def refresh(f: dict) -> int:
    """Add any books a followed author or series has gained since we last looked."""
    _, image, books = await _books_for(f["kind"], f["asin"], f["name"])
    known = set(f["known"])
    added = sum(1 for b in books if b["asin"] not in known and add_wanted(b))
    db.update_follow(f["id"], known=sorted(known | {b["asin"] for b in books}), checked_at=time.time(),
                     image=image or f["image"])
    return added


async def refresh_due() -> int:
    added = 0
    for f in db.list_follows():
        if time.time() - (f["checked_at"] or 0) >= FOLLOW_REFRESH_SECONDS:
            try:
                added += await refresh(f)
            except audible.AudibleError as e:
                log.info("Couldn't refresh %s: %s", f["name"], e)
    return added


def follow_counts(f: dict, books: list[dict]) -> dict:
    if f["kind"] == "author":
        mine = [b for b in books if normalize(f["name"]) in normalize(b["author"])]
    else:
        mine = [b for b in books if any(s.get("asin") == f["asin"] for s in b.get("series") or [])]
    return {
        "in_library": sum(b["status"] == "completed" for b in mine),
        "wanted": sum(b["status"] not in ("completed",) for b in mine),
    }
