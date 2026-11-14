"""Registers the pipeline steps on the worker, each with its fixed cadence."""
from __future__ import annotations

import logging
import time

from . import acquire, catalog, notify, sources
from .db import DB
from .metadata import cache
from .models import WEEKLY, State
from .repair import plex_issues, upgrades
from .worker import Worker

log = logging.getLogger("clid2.pipeline")

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def register(worker: Worker, db: DB) -> None:
    @worker.every(10 * MINUTE)
    async def sync_sources() -> None:
        await sources.sync_all(db)

    @worker.every(0)
    async def search_wanted() -> None:
        await acquire.search_wanted(db)

    @worker.every(30)
    async def check_downloads() -> None:
        await acquire.check_downloads(db)

    @worker.every(MINUTE)
    async def wake_waiting() -> None:
        """Move Waiting items whose time has come back to Wanted, and give
        Failed items a weekly second chance."""
        for item in db.items_in(State.WAITING, due_only=True, limit=1000):
            db.set_state(item.id, State.WANTED, next_attempt_at=None)
        now = time.time()
        for item in db.items_in(State.FAILED, due_only=True, limit=1000):
            if item.next_attempt_at is None:
                # First time seen in Failed: schedule the weekly retry.
                db.set_state(item.id, State.FAILED, next_attempt_at=now + WEEKLY)
            else:
                db.set_state(item.id, State.WANTED, next_attempt_at=None, attempts=0)
                db.log_event(f"Weekly retry: {item.label}", item.id)

    @worker.every(MINUTE)
    async def plex_reported_issues() -> None:
        await plex_issues.process(db)

    @worker.every(DAY)
    async def upgrade_sweep() -> None:
        await upgrades.sweep(db)

    @worker.every(30)
    async def send_notifications() -> None:
        await notify.flush(db)

    @worker.every(10 * MINUTE)
    async def backfill_posters() -> None:
        await catalog.backfill_posters(db)

    @worker.every(DAY)
    async def refresh_metadata() -> None:
        await catalog.refresh_all_shows(db)
        cache.purge_expired(db)
