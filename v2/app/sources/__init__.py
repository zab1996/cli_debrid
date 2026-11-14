"""Content sources: each returns what it wants as Request(kind, tmdb_id).

A request is acted on once, the first time it is seen (tracked in the
`requests` table). Deleting something in the app therefore sticks even if it
is still on the list. Items that leave a list are left alone.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .. import catalog, config
from ..config import SourceConfig
from ..db import DB
from ..metadata import tmdb

log = logging.getLogger("clid2.sources")


@dataclass(frozen=True)
class Request:
    kind: str          # "movie" | "show"
    tmdb_id: int
    title: str = ""


def source_key(s: SourceConfig) -> str:
    return f"{s.type}:{s.value}" if s.value else s.type


async def resolve_ids(db: DB, kind: str, tmdb_id=None, imdb_id=None, tvdb_id=None,
                      title: str = "") -> Request | None:
    """Turn whatever IDs a list gives us into a TMDB-keyed Request."""
    if tmdb_id:
        return Request(kind, int(tmdb_id), title)
    found = None
    if imdb_id:
        found = await tmdb.find_by_imdb(db, imdb_id)
    if not found and tvdb_id:
        found = await tmdb.find_by_tvdb(db, int(tvdb_id))
    if found:
        return Request(found["kind"], found["tmdb_id"], title)
    return None


async def fetch(db: DB, s: SourceConfig) -> list[Request]:
    from . import mdblist, plex_watchlist, trakt
    mod = {"plex_watchlist": plex_watchlist, "trakt_list": trakt, "trakt_watchlist": trakt,
           "mdblist": mdblist}[s.type]
    return await mod.fetch(db, s)


def _is_new(db: DB, key: str, r: Request) -> bool:
    with db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO requests(source, kind, tmdb_id, title, first_seen)"
                        " VALUES (?,?,?,?,?)", (key, r.kind, r.tmdb_id, r.title, time.time()))
        return cur.rowcount == 1


async def sync_all(db: DB) -> None:
    for s in config.get().sources:
        if not s.enabled:
            continue
        key = source_key(s)
        try:
            reqs = await fetch(db, s)
        except Exception as e:
            log.warning("source %s failed: %s", key, e)
            continue
        added = 0
        for r in reqs:
            if not _is_new(db, key, r):
                continue
            try:
                if r.kind == "movie":
                    added += bool(await catalog.add_movie(db, r.tmdb_id, source=key))
                else:
                    await catalog.add_show(db, r.tmdb_id, source=key)
                    added += 1
            except Exception as e:
                log.warning("adding %s %s from %s failed: %s", r.kind, r.tmdb_id, key, e)
                with db.tx() as c:  # retry next sync
                    c.execute("DELETE FROM requests WHERE source=? AND kind=? AND tmdb_id=?",
                              (key, r.kind, r.tmdb_id))
        if added:
            log.info("source %s: %d new", key, added)
