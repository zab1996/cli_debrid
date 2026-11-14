from __future__ import annotations

import re

from .. import http
from ..config import IndexerConfig
from ..ranking import Query, Release

BASE = "https://torrentio.strem.fun"
OPTS = "sort=qualitysize|qualityfilter=480p,scr,cam"
_SIZE = re.compile(r"💾\s*([\d.]+)\s*(TB|GB|MB|KB)", re.IGNORECASE)
_SEEDS = re.compile(r"👤\s*(\d+)")
_UNIT = {"TB": 1024.0, "GB": 1.0, "MB": 1 / 1024, "KB": 1 / 1024 ** 2}


def url_for(idx: IndexerConfig, q: Query) -> str | None:
    if not q.imdb_id:
        return None
    base = (idx.url or BASE).rstrip("/")
    if q.kind == "movie":
        return f"{base}/{OPTS}/stream/movie/{q.imdb_id}.json"
    ep = q.episode if q.kind == "episode" else 1
    return f"{base}/{OPTS}/stream/series/{q.imdb_id}:{q.season}:{ep}.json"


def parse(data: dict, name: str) -> list[Release]:
    out = []
    for s in data.get("streams", []):
        h = (s.get("infoHash") or "").lower()
        raw = s.get("title") or ""
        if not h or not raw:
            continue
        lines = raw.split("\n")
        size = 0.0
        seeds = 0
        for line in lines[1:]:
            m = _SIZE.search(line)
            if m:
                size = float(m.group(1)) * _UNIT[m.group(2).upper()]
            m = _SEEDS.search(line)
            if m:
                seeds = int(m.group(1))
        out.append(Release(title=lines[0].strip(), protocol="torrent", indexer=name,
                           size_gb=round(size, 2), seeders=seeds, info_hash=h))
    return out


async def search(idx: IndexerConfig, q: Query) -> list[Release]:
    url = url_for(idx, q)
    if not url:
        return []
    return parse(await http.get_json(url), "torrentio")
