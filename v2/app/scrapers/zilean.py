from __future__ import annotations

from .. import http
from ..config import IndexerConfig
from ..ranking import Query, Release


def params_for(q: Query) -> dict:
    p: dict = {"Query": q.title}
    if q.kind == "movie":
        if q.year:
            p["Year"] = q.year
    else:
        p["Season"] = q.season
        if q.kind == "episode":
            p["Episode"] = q.episode
    return p


def parse(data: list) -> list[Release]:
    out = []
    for it in data or []:
        h = (it.get("info_hash") or "").lower()
        if not h:
            continue
        try:
            size = float(it.get("size") or 0) / 1024 ** 3
        except (TypeError, ValueError):
            size = 0.0
        out.append(Release(title=it.get("raw_title") or "", protocol="torrent", indexer="zilean",
                           size_gb=round(size, 2), info_hash=h))
    return out


async def search(idx: IndexerConfig, q: Query) -> list[Release]:
    if not idx.url:
        return []
    data = await http.get_json(f"{idx.url.rstrip('/')}/dmm/filtered", params=params_for(q))
    return parse(data)
