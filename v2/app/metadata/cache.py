"""Tiny TTL cache over the meta_cache table."""
from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from ..db import DB

HOUR = 3600
DAY = 24 * HOUR


def get(db: DB, key: str) -> Any | None:
    rows = db.query("SELECT value, expires_at FROM meta_cache WHERE key=?", (key,))
    if not rows or rows[0]["expires_at"] < time.time():
        return None
    return json.loads(rows[0]["value"])


def put(db: DB, key: str, value: Any, ttl: float) -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO meta_cache(key, value, expires_at) VALUES (?,?,?)",
                  (key, json.dumps(value), time.time() + ttl))


async def cached(db: DB, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
    hit = get(db, key)
    if hit is not None:
        return hit
    value = await fetch()
    if value is not None:
        put(db, key, value, ttl)
    return value


def drop(db: DB, key: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM meta_cache WHERE key=?", (key,))


def purge_expired(db: DB) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM meta_cache WHERE expires_at < ?", (time.time(),))
