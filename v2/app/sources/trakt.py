"""Public Trakt lists and watchlists (needs only a Trakt app client id).

value examples:
  trakt_list:      "https://trakt.tv/users/alice/lists/scifi"  or  "alice/scifi"
  trakt_watchlist: "alice"
"""
from __future__ import annotations

import re

from .. import config, http
from ..config import SourceConfig
from ..db import DB
from . import Request, resolve_ids

API = "https://api.trakt.tv"
_LIST_URL = re.compile(r"users/([^/]+)/lists/([^/?#]+)")


def list_path(s: SourceConfig) -> str:
    v = s.value.strip().rstrip("/")
    if s.type == "trakt_watchlist":
        user = v.split("users/")[-1].split("/")[0]
        return f"/users/{user}/watchlist"
    m = _LIST_URL.search(v)
    if m:
        return f"/users/{m.group(1)}/lists/{m.group(2)}/items"
    user, _, slug = v.partition("/")
    if not slug:
        raise ValueError(f"Trakt list should look like user/list-slug, got {v!r}")
    return f"/users/{user}/lists/{slug}/items"


async def fetch(db: DB, s: SourceConfig) -> list[Request]:
    cid = config.get().trakt_client_id
    if not cid:
        raise ValueError("Trakt client id is not set")
    headers = {"trakt-api-version": "2", "trakt-api-key": cid}
    out: list[Request] = []
    page = 1
    while page <= 50:
        resp = await http.request("GET", f"{API}{list_path(s)}",
                                  params={"page": page, "limit": 100}, headers=headers)
        resp.raise_for_status()
        rows = resp.json()
        for row in rows:
            kind = row.get("type")
            if kind not in ("movie", "show"):
                continue
            obj = row.get(kind) or {}
            ids = obj.get("ids") or {}
            r = await resolve_ids(db, kind, ids.get("tmdb"), ids.get("imdb"), ids.get("tvdb"),
                                  obj.get("title", ""))
            if r:
                out.append(r)
        if page >= int(resp.headers.get("X-Pagination-Page-Count", page)):
            break
        page += 1
    return out
