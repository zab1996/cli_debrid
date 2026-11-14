"""Prowlarr: returns both torrents and usenet results."""
from __future__ import annotations

import asyncio

from .. import http
from ..config import IndexerConfig
from ..ranking import Query, Release
from . import hash_from_magnet
from .jackett import query_text


def params_list(q: Query) -> list[dict]:
    base = {"limit": 500, "offset": 0}
    out = []
    kind = "movie" if q.kind == "movie" else "tvsearch"
    if q.imdb_id:
        tokens = f"{{ImdbId:{q.imdb_id}}}"
        if q.kind != "movie":
            tokens += f"{{Season:{q.season}}}"
            if q.kind == "episode":
                tokens += f"{{Episode:{q.episode}}}"
        out.append({**base, "type": kind, "query": tokens})
    out.append({**base, "type": "search", "query": query_text(q)})
    return out


def parse(data: list) -> list[Release]:
    out = []
    for it in data or []:
        proto = (it.get("protocol") or "torrent").lower()
        title = it.get("title") or ""
        size = round((it.get("size") or 0) / 1024 ** 3, 2)
        name = f"prowlarr:{it.get('indexer') or ''}"
        imdb = it.get("imdbId")
        imdb_s = f"tt{int(imdb):07d}" if imdb else ""
        if proto in ("usenet", "nzb"):
            url = it.get("downloadUrl") or ""
            if url:
                out.append(Release(title=title, protocol="nzb", indexer=name, size_gb=size,
                                   nzb_url=url, guid=it.get("guid") or url, indexer_imdb=imdb_s))
            continue
        magnet = it.get("magnetUrl") or ""
        guid = it.get("guid") or ""
        if not magnet and guid.startswith("magnet:"):
            magnet = guid
        h = (it.get("infoHash") or "").lower() or hash_from_magnet(magnet)
        if h:
            out.append(Release(title=title, protocol="torrent", indexer=name, size_gb=size,
                               seeders=it.get("seeders") or 0, info_hash=h, magnet=magnet,
                               indexer_imdb=imdb_s))
    return out


async def search(idx: IndexerConfig, q: Query) -> list[Release]:
    if not (idx.url and idx.api_key):
        return []
    url = f"{idx.url.rstrip('/')}/api/v1/search"
    headers = {"X-Api-Key": idx.api_key}

    async def one(p: dict) -> list[Release]:
        return parse(await http.get_json(url, params=p, headers=headers, timeout=40))
    results = await asyncio.gather(*(one(p) for p in params_list(q)), return_exceptions=True)
    return [r for rs in results if isinstance(rs, list) for r in rs]
