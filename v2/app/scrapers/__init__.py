"""Indexers. Each module exposes `async search(idx, q) -> list[Release]`."""
from __future__ import annotations

import asyncio
import base64
import logging
import re

from .. import config
from ..config import IndexerConfig
from ..ranking import Query, Release

log = logging.getLogger("clid2.scrapers")

PER_INDEXER_TIMEOUT = 45
_BTIH = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
)


def hash_from_magnet(magnet: str) -> str:
    m = _BTIH.search(magnet or "")
    if not m:
        return ""
    h = m.group(1)
    if len(h) == 32:
        h = base64.b32decode(h.upper()).hex()
    return h.lower()


def magnet_for(info_hash: str, name: str = "") -> str:
    from urllib.parse import quote
    tr = "".join(f"&tr={quote(t, safe='')}" for t in TRACKERS)
    dn = f"&dn={quote(name)}" if name else ""
    return f"magnet:?xt=urn:btih:{info_hash}{dn}{tr}"


def _module(kind: str):
    from . import jackett, newznab, prowlarr, torrentio, zilean
    return {"torrentio": torrentio, "zilean": zilean, "jackett": jackett,
            "prowlarr": prowlarr, "newznab": newznab}[kind]


def label(idx: IndexerConfig) -> str:
    from urllib.parse import urlparse
    host = urlparse(idx.url).hostname if idx.url else ""
    return f"{idx.type}:{host}" if host else idx.type


async def search_one(idx: IndexerConfig, q: Query) -> list[Release]:
    try:
        return await asyncio.wait_for(_module(idx.type).search(idx, q), PER_INDEXER_TIMEOUT)
    except Exception as e:
        log.warning("%s search failed for %s: %s", label(idx), q.title, e or type(e).__name__)
        return []


async def search_all(q: Query) -> list[Release]:
    cfg = config.get()
    want_torrents = bool(cfg.debrid.provider and cfg.debrid.api_key)
    want_nzb = bool(cfg.usenet)
    idxs = [i for i in cfg.indexers if i.enabled]
    results = await asyncio.gather(*(search_one(i, q) for i in idxs))
    out: list[Release] = []
    for rs in results:
        for r in rs:
            if r.protocol == "torrent" and want_torrents and r.info_hash:
                out.append(r)
            elif r.protocol == "nzb" and want_nzb and r.nzb_url:
                out.append(r)
    return out
