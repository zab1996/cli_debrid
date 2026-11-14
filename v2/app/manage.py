"""User actions from the UI: retry, replace, delete."""
from __future__ import annotations

import logging

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
        symlink.remove(it.file_path)
        with db.tx() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
        for eid in (it.engine_id, it.prev_engine_id):
            await _drop_download(db, eid)
        n += 1
    return n


async def delete_show(db: DB, tmdb_id: int) -> int:
    ids = [r["id"] for r in db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=?",
                                     (tmdb_id,))]
    n = await delete_items(db, ids)
    title = db.query("SELECT title FROM shows WHERE tmdb_id=?", (tmdb_id,))
    with db.tx() as c:
        c.execute("DELETE FROM shows WHERE tmdb_id=?", (tmdb_id,))
    if title:
        db.log_event(f"Deleted {title[0]['title']} ({n} episodes)")
    return n
