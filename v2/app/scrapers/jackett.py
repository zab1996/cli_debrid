from __future__ import annotations

from .. import http
from ..config import IndexerConfig
from ..ranking import Query, Release
from . import hash_from_magnet


def query_text(q: Query) -> str:
    if q.kind == "movie":
        return f"{q.title} {q.year or ''}".strip()
    if q.kind == "episode":
        return f"{q.title} S{q.season:02d}E{q.episode:02d}"
    return f"{q.title} S{q.season:02d}"


def parse(data: dict) -> list[Release]:
    out = []
    for it in data.get("Results", []):
        magnet = it.get("MagnetUri") or ""
        link = it.get("Link") or ""
        if not magnet and link.startswith("magnet:"):
            magnet = link
        h = (it.get("InfoHash") or "").lower() or hash_from_magnet(magnet)
        if not h:
            continue  # .torrent-file-only results need a download; skipped for now
        imdb = it.get("Imdb")
        out.append(Release(
            title=it.get("Title") or "", protocol="torrent",
            indexer=f"jackett:{it.get('Tracker') or ''}",
            size_gb=round((it.get("Size") or 0) / 1024 ** 3, 2),
            seeders=it.get("Seeders") or 0, info_hash=h, magnet=magnet,
            indexer_imdb=f"tt{int(imdb):07d}" if imdb else "",
        ))
    return out


async def search(idx: IndexerConfig, q: Query) -> list[Release]:
    if not (idx.url and idx.api_key):
        return []
    data = await http.get_json(
        f"{idx.url.rstrip('/')}/api/v2.0/indexers/all/results",
        params={"apikey": idx.api_key, "Query": query_text(q)},
        timeout=40,
    )
    return parse(data)
