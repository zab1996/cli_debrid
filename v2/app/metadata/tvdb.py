"""TVDB v4: aired-order episodes with absolute numbers.

Used for shows when a TVDB key is set; TMDB is the fallback. TVDB's aired
order is what most release groups follow, and absolute numbers matter for
anime packs.
"""
from __future__ import annotations

import time
from typing import Any

from .. import config, http
from ..db import DB
from .cache import HOUR, cached

BASE = "https://api4.thetvdb.com/v4"
_token: tuple[str, str, float] | None = None  # (api_key, token, expires)


class TVDBError(Exception):
    pass


async def _token_for(key: str) -> str:
    global _token
    if _token and _token[0] == key and _token[2] > time.time():
        return _token[1]
    resp = await http.request("POST", f"{BASE}/login", json={"apikey": key})
    if resp.status_code != 200:
        raise TVDBError(f"TVDB login failed ({resp.status_code})")
    tok = resp.json()["data"]["token"]
    _token = (key, tok, time.time() + 25 * 24 * 3600)
    return tok


async def _get(path: str, **params: Any) -> Any:
    key = config.get().tvdb_api_key
    if not key:
        raise TVDBError("TVDB API key is not set")
    tok = await _token_for(key)
    return await http.get_json(f"{BASE}{path}", params=params,
                               headers={"Authorization": f"Bearer {tok}"})


def enabled() -> bool:
    return bool(config.get().tvdb_api_key)


async def episodes(db: DB, tvdb_id: int) -> dict[str, list[dict]]:
    """{"1": [{"episode", "air_date", "title", "absolute"}]}, specials excluded."""
    async def fetch():
        seasons: dict[str, list[dict]] = {}
        page = 0
        while page < 50:
            d = await _get(f"/series/{tvdb_id}/episodes/default/eng", page=page)
            eps = (d.get("data") or {}).get("episodes") or []
            for e in eps:
                sn, num = e.get("seasonNumber"), e.get("number")
                if not sn or num is None:
                    continue
                seasons.setdefault(str(sn), []).append({
                    "episode": num,
                    "air_date": (e.get("aired") or None),
                    "title": e.get("name") or "",
                    "absolute": e.get("absoluteNumber") or None,
                    "runtime": e.get("runtime") or 0,
                })
            if not eps or not (d.get("links") or {}).get("next"):
                break
            page += 1
        for v in seasons.values():
            v.sort(key=lambda e: e["episode"])
        return seasons
    return await cached(db, f"tvdb:episodes:{tvdb_id}", 12 * HOUR, fetch)
