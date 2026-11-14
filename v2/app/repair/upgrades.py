"""Daily: look for a clearly better release for recently finished items."""
from __future__ import annotations

import logging
import time

from .. import acquire, config, search
from ..db import DB
from ..models import State

log = logging.getLogger("clid2.upgrades")

PER_DAY = 40


async def sweep(db: DB) -> None:
    days = config.get().upgrade_days
    if days <= 0:
        return
    since = time.time() - days * 86400
    rows = db.query("SELECT id FROM items WHERE state=? AND mode IS NULL AND done_at>=?"
                    " ORDER BY done_at DESC LIMIT ?", (State.DONE.value, since, PER_DAY))
    for r in rows:
        it = db.get_item(r["id"])
        if not it:
            continue
        try:
            await acquire.run_job(db, search.Job(search.query_for(db, it), [it]), upgrade=True)
        except Exception as e:
            log.warning("upgrade search for %s failed: %s", it.label, e)
