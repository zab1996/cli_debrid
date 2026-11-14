"""MDBList lists.

value: a list URL ("https://mdblist.com/lists/alice/top-movies") or a list id.
With an MDBList API key the API is used; without one, the list's public
JSON export is read.
"""
from __future__ import annotations

from urllib.parse import quote

from .. import config, http
from ..config import SourceConfig
from ..db import DB
from . import Request, resolve_ids

API = "https://api.mdblist.com"


def _api_url(value: str) -> str:
    v = value.strip().rstrip("/")
    if "mdblist.com/lists/" in v:
        user, _, name = v.split("mdblist.com/lists/")[1].partition("/")
        return f"{API}/lists/{quote(user)}/{quote(name.split('/')[0])}/items"
    return f"{API}/lists/{quote(v)}/items"


def _public_url(value: str) -> str:
    v = value.strip().rstrip("/")
    if not v.startswith("http"):
        v = f"https://mdblist.com/lists/{v}"
    return v if v.endswith("/json") else f"{v}/json"


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        out = []
        for bucket, kind in (("movies", "movie"), ("shows", "show")):
            for r in payload.get(bucket) or []:
                r.setdefault("mediatype", kind)
                out.append(r)
        return out
    return []


async def fetch(db: DB, s: SourceConfig) -> list[Request]:
    key = config.get().mdblist_api_key
    if key:
        rows: list[dict] = []
        offset = 0
        for _ in range(50):
            resp = await http.request("GET", _api_url(s.value),
                                      params={"apikey": key, "limit": 500, "offset": offset})
            resp.raise_for_status()
            page = _rows(resp.json())
            rows += page
            if not page or str(resp.headers.get("X-Has-More", "")).lower() not in ("1", "true"):
                break
            offset += len(page)
    else:
        rows = _rows(await http.get_json(_public_url(s.value)))
    out: list[Request] = []
    for r in rows:
        kind = "show" if r.get("mediatype") in ("show", "tv") else "movie"
        ids = r.get("ids") or {}
        tmdb_id = ids.get("tmdb") or r.get("tmdb_id")
        imdb_id = ids.get("imdb") or r.get("imdb_id")
        tvdb_id = ids.get("tvdb") or r.get("tvdbid") or r.get("tvdb_id")
        req = await resolve_ids(db, kind, tmdb_id, imdb_id, tvdb_id, r.get("title", ""))
        if req:
            out.append(req)
    return out
