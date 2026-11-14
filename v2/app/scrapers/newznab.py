"""Newznab usenet indexers (NZBGeek, NZBFinder, DrunkenSlug, ...)."""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from .. import http
from ..config import IndexerConfig
from ..ranking import Query, Release
from .jackett import query_text

CAT_MOVIE = "2000,2030,2040,2045,2050,2060"
CAT_TV = "5000,5010,5020,5030,5040,5045,5060,5070"
NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}


def params_list(idx: IndexerConfig, q: Query) -> list[dict]:
    base = {"apikey": idx.api_key, "limit": 100, "extended": 1}
    out = []
    imdb = (q.imdb_id or "").removeprefix("tt")
    if q.kind == "movie":
        if imdb:
            out.append({**base, "t": "movie", "imdbid": imdb, "cat": CAT_MOVIE})
        out.append({**base, "t": "search", "q": query_text(q), "cat": CAT_MOVIE})
    else:
        if imdb:
            p = {**base, "t": "tvsearch", "imdbid": imdb, "season": q.season, "cat": CAT_TV}
            if q.kind == "episode":
                p["ep"] = q.episode
            out.append(p)
        out.append({**base, "t": "search", "q": query_text(q), "cat": CAT_TV})
    return out


def parse(xml_text: str, indexer: str) -> list[Release]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ch = root.find("channel")
    if ch is None:
        return []
    out = []
    for it in ch.findall("item"):
        title = (it.findtext("title") or "").strip()
        enc = it.find("enclosure")
        url = (enc.get("url") if enc is not None else "") or (it.findtext("link") or "").strip()
        if not title or not url:
            continue
        guid = (it.findtext("guid") or url).strip()
        size = 0
        imdb = ""
        if enc is not None and (enc.get("length") or "").isdigit():
            size = int(enc.get("length"))
        for a in it.findall("newznab:attr", NS):
            name, val = a.get("name"), a.get("value") or ""
            if name == "size" and not size and val.isdigit():
                size = int(val)
            elif name == "imdb" and val.strip("0"):
                imdb = f"tt{int(val):07d}" if val.isdigit() else val
        out.append(Release(title=title, protocol="nzb", indexer=indexer,
                           size_gb=round(size / 1024 ** 3, 2), nzb_url=url, guid=guid,
                           indexer_imdb=imdb))
    return out


async def search(idx: IndexerConfig, q: Query) -> list[Release]:
    if not (idx.url and idx.api_key):
        return []
    base = idx.url.rstrip("/")
    endpoint = base if base.endswith("/api") else f"{base}/api"
    from . import label
    name = label(idx)

    async def one(p: dict) -> list[Release]:
        resp = await http.request("GET", endpoint, params=p, timeout=40)
        if resp.status_code != 200:
            return []
        return parse(resp.text, name)
    results = await asyncio.gather(*(one(p) for p in params_list(idx, q)), return_exceptions=True)
    return [r for rs in results if isinstance(rs, list) for r in rs]
