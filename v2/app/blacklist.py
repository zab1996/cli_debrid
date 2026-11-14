"""Title and release blacklists.

A blacklisted title is never added again: not by lists, not by the Add page,
and new episodes of a blacklisted show (or season) are skipped. A blacklisted
release is never picked again for any item.
"""
from __future__ import annotations

import time

from .db import DB

WHOLE = -1


def is_blocked(db: DB, kind: str, tmdb_id: int, season: int | None = None) -> bool:
    """kind is 'movie' or 'show'. With a season, a season-only block counts too."""
    seasons = (WHOLE,) if season is None else (WHOLE, season)
    marks = ",".join("?" * len(seasons))
    return bool(db.query(f"SELECT 1 FROM blacklist WHERE kind=? AND tmdb_id=? AND season IN ({marks})",
                         (kind, tmdb_id, *seasons)))


def block(db: DB, kind: str, tmdb_id: int, title: str = "", season: int | None = None,
          reason: str = "") -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO blacklist(kind, tmdb_id, season, title, reason, at)"
                  " VALUES (?,?,?,?,?,?)",
                  (kind, tmdb_id, WHOLE if season is None else season, title, reason, time.time()))
    what = title + (f" season {season}" if season is not None else "")
    db.log_event(f"Blacklisted {what}")
    from . import notify
    notify.emit(db, "blacklisted", what or str(tmdb_id), reason)


def unblock(db: DB, kind: str, tmdb_id: int, season: int | None = None) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM blacklist WHERE kind=? AND tmdb_id=? AND season=?",
                  (kind, tmdb_id, WHOLE if season is None else season))
        if kind == "movie" or season is None:
            # Let lists pick it up again on their next sync.
            c.execute("DELETE FROM requests WHERE kind=? AND tmdb_id=?", (kind, tmdb_id))


def titles(db: DB) -> list:
    return db.query("SELECT * FROM blacklist ORDER BY at DESC")


def block_release(db: DB, key: str, name: str = "", reason: str = "") -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO release_blacklist(release_key, name, reason, at)"
                  " VALUES (?,?,?,?)", (key.lower(), name, reason, time.time()))
    db.log_event(f"Blocked release {name or key}")


def unblock_release(db: DB, key: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM release_blacklist WHERE release_key=?", (key.lower(),))


def release_keys(db: DB) -> set[str]:
    return {r["release_key"] for r in db.query("SELECT release_key FROM release_blacklist")}


def releases(db: DB) -> list:
    return db.query("SELECT * FROM release_blacklist ORDER BY at DESC")
