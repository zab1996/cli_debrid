"""Registers the pipeline steps on the worker, each with its fixed cadence.

Phase 1: the steps exist and run on schedule but do nothing yet. Later
phases fill them in (search, download, library, sources, repair).
"""
from __future__ import annotations

import logging
import time

from . import catalog
from .db import DB
from .metadata import cache
from .models import WEEKLY, State
from .worker import Worker

log = logging.getLogger("clid2.pipeline")

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def register(worker: Worker, db: DB) -> None:
    @worker.every(10 * MINUTE)
    async def sync_sources() -> None:
        pass  # phase 7

    @worker.every(0)
    async def search_wanted() -> None:
        pass  # phase 3: Wanted -> Searching -> Downloading | Waiting

    @worker.every(0)
    async def check_downloads() -> None:
        pass  # phase 5: Downloading -> Done | back to Searching

    @worker.every(MINUTE)
    async def wake_waiting() -> None:
        """Move Waiting items whose time has come back to Wanted, and give
        Failed items a weekly second chance."""
        for item in db.items_in(State.WAITING, due_only=True):
            db.set_state(item.id, State.WANTED, next_attempt_at=None)
        now = time.time()
        for item in db.items_in(State.FAILED, due_only=True):
            if item.next_attempt_at is None:
                # First time seen in Failed: schedule the weekly retry.
                db.set_state(item.id, State.FAILED, next_attempt_at=now + WEEKLY)
            else:
                db.set_state(item.id, State.WANTED, next_attempt_at=None, attempts=0)
                db.log_event(f"Weekly retry: {item.title}", item.id)

    @worker.every(MINUTE)
    async def plex_reported_issues() -> None:
        pass  # phase 8

    @worker.every(DAY)
    async def upgrade_sweep() -> None:
        pass  # phase 8

    @worker.every(DAY)
    async def refresh_metadata() -> None:
        await catalog.refresh_all_shows(db)
        cache.purge_expired(db)
