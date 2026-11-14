"""Indexers. Each module exposes `async search(idx, q) -> list[Release]`."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time

from .. import config, http
from ..config import IndexerConfig
from ..ranking import Query, Release

log = logging.getLogger("clid2.scrapers")

PER_INDEXER_TIMEOUT = 45
# Requests per hour per indexer host. Keeps a big backlog from burning a daily
# API quota (NZBGeek etc.); only that indexer pauses when it's reached.
INDEXER_HOURLY_LIMIT = 300
_paused_logged: dict[str, float] = {}
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


def _apply_limits(cfg) -> None:
    """advanced: indexer_hourly_limit (all) and indexer_limits {host: n} (per host)."""
    from urllib.parse import urlparse
    default = int(cfg.adv("indexer_hourly_limit", INDEXER_HOURLY_LIMIT))
    per_host = cfg.adv("indexer_limits", {}) or {}
    for i in cfg.indexers:
        host = urlparse(i.url).hostname if i.url else ("torrentio.strem.fun" if i.type == "torrentio" else "")
        if host:
            http.limit_host(host, int(per_host.get(host, default)))


def _module(kind: str):
    from . import jackett, newznab, prowlarr, torrentio, zilean
    return {"torrentio": torrentio, "zilean": zilean, "jackett": jackett,
            "prowlarr": prowlarr, "newznab": newznab}[kind]


def label(idx: IndexerConfig) -> str:
    from urllib.parse import urlparse
    host = urlparse(idx.url).hostname if idx.url else ""
    return f"{idx.type}:{host}" if host else idx.type


async def search_one(idx: IndexerConfig, q: Query) -> list[Release]:
    started = time.monotonic()
    try:
        out = await asyncio.wait_for(_module(idx.type).search(idx, q), PER_INDEXER_TIMEOUT)
    except http.RateLimited as e:
        host = label(idx)
        if time.time() - _paused_logged.get(host, 0) > 600:
            _paused_logged[host] = time.time()
            log.warning("[indexer %s] paused: %s; it resumes on its own", host, e)
        return []
    except asyncio.TimeoutError:
        log.warning("[indexer %s] timed out after %ds for %s", label(idx), PER_INDEXER_TIMEOUT, _qlabel(q))
        return []
    except Exception as e:
        log.warning("[indexer %s] search failed for %s: %s: %s", label(idx), _qlabel(q),
                    type(e).__name__, e)
        return []
    log.debug("[indexer %s] %d results for %s in %.1fs", label(idx), len(out), _qlabel(q),
              time.monotonic() - started)
    return out


def _qlabel(q: Query) -> str:
    if q.kind == "movie":
        return f"{q.title} ({q.year})"
    if q.kind == "season":
        return f"{q.title} S{q.season:02d} pack"
    return f"{q.title} S{q.season:02d}E{q.episode:02d}"


async def search_all(q: Query) -> list[Release]:
    cfg = config.get()
    want_torrents = bool(cfg.debrid.provider and cfg.debrid.api_key)
    want_nzb = bool(cfg.usenet)
    idxs = [i for i in cfg.indexers if i.enabled]
    _apply_limits(cfg)
    results = await asyncio.gather(*(search_one(i, q) for i in idxs))
    out: list[Release] = []
    per = {label(i): len(rs) for i, rs in zip(idxs, results)}
    log.info("searched %s: %s", _qlabel(q), ", ".join(f"{k}={v}" for k, v in per.items()) or "no indexers")
    dropped = 0
    for rs in results:
        for r in rs:
            if r.protocol == "torrent" and want_torrents and r.info_hash:
                out.append(r)
            elif r.protocol == "nzb" and want_nzb and r.nzb_url:
                out.append(r)
            else:
                dropped += 1
    if dropped:
        log.debug("%d results dropped (no debrid/usenet configured for their type)", dropped)
    return out
