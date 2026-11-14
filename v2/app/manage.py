"""User actions from the UI: retry, replace, delete."""
from __future__ import annotations

import logging

from . import blacklist
from .db import DB
from .downloads import engine
from .library import symlink
from .models import State
from .repair.plex_issues import start_replace

log = logging.getLogger("clid2.manage")


def retry(db: DB, item_id: int) -> bool:
    it = db.get_item(item_id)
    if not it or it.state not in (State.FAILED, State.WAITING):
        return False
    db.set_state(item_id, State.WANTED, attempts=0, next_attempt_at=None)
    db.log_event(f"Retry requested: {it.label}", item_id)
    return True


def replace(db: DB, item_id: int) -> bool:
    return start_replace(db, item_id, "requested in the app")


async def _drop_download(db: DB, engine_id: str | None) -> None:
    if engine_id and not db.engine_id_in_use(engine_id):
        try:
            await engine.delete(engine_id)
        except engine.EngineError as e:
            log.warning("could not delete download %s: %s", engine_id, e)


async def delete_items(db: DB, item_ids: list[int]) -> int:
    """Remove items, their library links and (if unused) their downloads."""
    n = 0
    for item_id in item_ids:
        it = db.get_item(item_id)
        if not it:
            continue
        log.info("%s deleted by user (state %s, file %s)", it.tag, it.state.value, it.file_path or "-")
        symlink.remove(it.file_path)
        with db.tx() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
            if it.kind == "episode":
                c.execute("INSERT OR IGNORE INTO skipped_episodes VALUES (?,?,?)",
                          (it.tmdb_id, it.season, it.episode))
        for eid in (it.engine_id, it.prev_engine_id):
            await _drop_download(db, eid)
        n += 1
    return n


async def delete_show(db: DB, tmdb_id: int, block: bool = False) -> int:
    ids = [r["id"] for r in db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=?",
                                     (tmdb_id,))]
    n = await delete_items(db, ids)
    rows = db.query("SELECT title FROM shows WHERE tmdb_id=?", (tmdb_id,))
    title = rows[0]["title"] if rows else str(tmdb_id)
    with db.tx() as c:
        c.execute("DELETE FROM shows WHERE tmdb_id=?", (tmdb_id,))
        c.execute("DELETE FROM skipped_episodes WHERE tmdb_id=?", (tmdb_id,))
    if block:
        blacklist.block(db, "show", tmdb_id, title, reason="deleted from library")
    db.log_event(f"Deleted {title} ({n} episodes)")
    return n


async def delete_season(db: DB, tmdb_id: int, season: int, block: bool = False) -> int:
    ids = [r["id"] for r in db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=?"
                                     " AND season=?", (tmdb_id, season))]
    n = await delete_items(db, ids)
    rows = db.query("SELECT title FROM shows WHERE tmdb_id=?", (tmdb_id,))
    title = rows[0]["title"] if rows else str(tmdb_id)
    if block:
        blacklist.block(db, "show", tmdb_id, title, season=season, reason="deleted from library")
    db.log_event(f"Deleted {title} season {season} ({n} episodes)")
    return n


async def delete_and_block(db: DB, item_ids: list[int]) -> int:
    """Delete items; movies get blacklisted, episodes blacklist their season."""
    seen: set[tuple] = set()
    for item_id in item_ids:
        it = db.get_item(item_id)
        if not it:
            continue
        key = ("movie", it.tmdb_id, None) if it.kind == "movie" else ("show", it.tmdb_id, it.season)
        if key not in seen:
            seen.add(key)
            blacklist.block(db, key[0], it.tmdb_id, it.title, season=key[2],
                            reason="deleted from library")
    return await delete_items(db, item_ids)


async def block_release(db: DB, item_id: int, release_key: str) -> bool:
    """Never pick this release again; if an item is using it, replace that item."""
    from . import acquire
    rows = db.query("SELECT name FROM releases_tried WHERE item_id=? AND release_key=?",
                    (item_id, release_key))
    name = rows[0]["name"] if rows else ""
    blacklist.block_release(db, release_key, name, reason="blocked by user")
    users = db.query("SELECT item_id FROM releases_tried WHERE release_key=? AND outcome='picked'",
                     (release_key,))
    downloading: dict[str, list] = {}
    for u in users:
        it = db.get_item(u["item_id"])
        if it and it.state is State.DONE:
            start_replace(db, it.id, "its release was blocked")
        elif it and it.state is State.DOWNLOADING and it.engine_id:
            downloading.setdefault(it.engine_id, []).append(it)
    for engine_id, items in downloading.items():
        await acquire._fail(db, items, engine_id, "its release was blocked")
    return True


BULK_ACTIONS = ("delete", "delete_block", "retry", "replace", "wanted")


async def bulk(db: DB, action: str, item_ids: list[int]) -> int:
    if action == "delete":
        return await delete_items(db, item_ids)
    if action == "delete_block":
        return await delete_and_block(db, item_ids)
    n = 0
    for i in item_ids:
        if action == "retry":
            n += retry(db, i)
        elif action == "replace":
            n += replace(db, i)
        elif action == "wanted":
            it = db.get_item(i)
            if it and it.state in (State.WAITING, State.FAILED, State.WANTED):
                db.set_state(i, State.WANTED, attempts=0, next_attempt_at=None)
                n += 1
    return n
