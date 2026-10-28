"""SQLite storage for the user's library."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

# Lifecycle of a book:
#   wanted -> queued -> resolving -> caching -> downloading -> organizing -> completed
#                                     any step can land in -> failed
STATUSES = ("wanted", "queued", "resolving", "caching", "downloading", "organizing", "completed", "failed")
ACTIVE = ("queued", "resolving", "caching", "downloading", "organizing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    narrators     TEXT NOT NULL DEFAULT '[]',
    cover_url     TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT '',
    categories    TEXT NOT NULL DEFAULT '[]',
    format        TEXT NOT NULL DEFAULT '',
    bitrate       TEXT NOT NULL DEFAULT '',
    length        TEXT NOT NULL DEFAULT '',
    size          TEXT NOT NULL DEFAULT '',
    info_hash     TEXT NOT NULL DEFAULT '',
    trackers      TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'wanted',
    progress      REAL NOT NULL DEFAULT 0,
    speed         INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    rd_torrent_id TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    added_at      REAL NOT NULL,
    updated_at    REAL NOT NULL,
    completed_at  REAL
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = {
    "listen_position": "REAL NOT NULL DEFAULT 0",
    "listen_duration": "REAL NOT NULL DEFAULT 0",
    "listened_at": "REAL",
    "finished": "INTEGER NOT NULL DEFAULT 0",
    "files": "TEXT NOT NULL DEFAULT '[]'",
    "protocol": "TEXT NOT NULL DEFAULT ''",
    "download_url": "TEXT NOT NULL DEFAULT ''",
    "sab_nzo_id": "TEXT NOT NULL DEFAULT ''",
    "indexer": "TEXT NOT NULL DEFAULT ''",
    "release": "TEXT NOT NULL DEFAULT ''",
}

JSON_COLUMNS = ("narrators", "categories", "trackers", "files")


def db_path() -> Path:
    return config.DATA_DIR / "library.db"


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
        for col, ddl in MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE books ADD COLUMN {col} {ddl}")
        # Anything mid-flight when the app last stopped goes back in the queue.
        conn.execute(
            f"UPDATE books SET status='queued', speed=0 WHERE status IN ({','.join('?' * len(ACTIVE))})",
            ACTIVE,
        )


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        d[col] = json.loads(d[col] or "[]")
    d["finished"] = bool(d.get("finished"))
    return d


def list_books() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM books ORDER BY added_at DESC").fetchall()
    return [_row(r) for r in rows]


def get(book_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())


def get_by_url(url: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM books WHERE source_url=?", (url,)).fetchone())


def insert(book: dict) -> dict:
    now = time.time()
    data = {**book, "added_at": now, "updated_at": now}
    for col in JSON_COLUMNS:
        if col in data:
            data[col] = json.dumps(data[col])
    cols = ", ".join(data)
    marks = ", ".join("?" * len(data))
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO books ({cols}) VALUES ({marks})", tuple(data.values()))
        book_id = cur.lastrowid
    return get(book_id)


def update(book_id: int, **changes) -> None:
    changes["updated_at"] = time.time()
    for col in JSON_COLUMNS:
        if col in changes:
            changes[col] = json.dumps(changes[col])
    sets = ", ".join(f"{k}=?" for k in changes)
    with connect() as conn:
        conn.execute(f"UPDATE books SET {sets} WHERE id=?", (*changes.values(), book_id))


def delete(book_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))


def next_queued(exclude: set[int]) -> dict | None:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM books WHERE status='queued' ORDER BY updated_at ASC").fetchall()
    for r in rows:
        if r["id"] not in exclude:
            return _row(r)
    return None


def stats() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM books GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["n"]
    return {
        "total": sum(counts.values()),
        "completed": counts["completed"],
        "active": sum(counts[s] for s in ACTIVE),
        "wanted": counts["wanted"],
        "failed": counts["failed"],
    }
