"""The Plex account's own watchlist (needs only the Plex token)."""
from __future__ import annotations

import logging

from .. import config, http
from ..config import SourceConfig
from ..db import DB
from ..metadata.cache import DAY, cached
from . import Request, resolve_ids

log = logging.getLogger("clid2.sources.plex")

DISCOVER = "https://discover.provider.plex.tv"
PAGE = 100


def _headers(token: str) -> dict:
    return {"X-Plex-Token": token, "Accept": "application/json"}


def ids_from_guids(guids: list[dict]) -> dict:
    out = {}
    for g in guids or []:
        scheme, _, val = (g.get("id") or "").partition("://")
        if scheme in ("imdb", "tmdb", "tvdb") and val:
            out[scheme] = val
    return out


async def _item_ids(db: DB, token: str, rating_key: str) -> dict:
    async def fetch():
        d = await http.get_json(f"{DISCOVER}/library/metadata/{rating_key}",
                                params={"includeGuids": 1}, headers=_headers(token))
        md = (d.get("MediaContainer", {}).get("Metadata") or [{}])[0]
        return ids_from_guids(md.get("Guid") or [])
    return await cached(db, f"plex:guids:{rating_key}", 30 * DAY, fetch) or {}


async def fetch(db: DB, s: SourceConfig) -> list[Request]:
    token = config.get().plex.token
    if not token:
        return []
    out: list[Request] = []
    start = 0
    while start < 5000:
        d = await http.get_json(
            f"{DISCOVER}/library/sections/watchlist/all",
            params={"includeGuids": 1, "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": PAGE},
            headers=_headers(token))
        mc = d.get("MediaContainer", {})
        meta = mc.get("Metadata") or []
        for m in meta:
            kind = "movie" if m.get("type") == "movie" else "show" if m.get("type") == "show" else None
            if not kind:
                continue
            ids = ids_from_guids(m.get("Guid") or [])
            if not ids and m.get("ratingKey"):
                ids = await _item_ids(db, token, m["ratingKey"])
            r = await resolve_ids(db, kind, ids.get("tmdb"), ids.get("imdb"), ids.get("tvdb"),
                                  m.get("title", ""))
            if r:
                out.append(r)
        start += len(meta)
        if not meta or start >= int(mc.get("totalSize", 0) or 0):
            break
    return out
