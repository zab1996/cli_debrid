"""Plex: find the library section for a path and ask for a partial scan."""
from __future__ import annotations

import logging
import time

from .. import config, http

log = logging.getLogger("clid2.plex")

_sections: tuple[float, list[tuple[str, str]]] | None = None  # (fetched_at, [(key, location)])
SECTIONS_TTL = 600


def enabled() -> bool:
    c = config.get().plex
    return bool(c.url and c.token)


def _headers() -> dict:
    return {"X-Plex-Token": config.get().plex.token, "Accept": "application/json"}


def plex_path(local: str) -> str:
    """Translate our path to Plex's view (advanced: plex_path_map {ours: theirs})."""
    for ours, theirs in (config.get().adv("plex_path_map", {}) or {}).items():
        if local.startswith(ours):
            return theirs + local[len(ours):]
    return local


async def sections(fresh: bool = False) -> list[tuple[str, str]]:
    global _sections
    if not fresh and _sections and time.time() - _sections[0] < SECTIONS_TTL:
        return _sections[1]
    base = config.get().plex.url.rstrip("/")
    data = await http.get_json(f"{base}/library/sections", headers=_headers())
    out = []
    for d in data.get("MediaContainer", {}).get("Directory", []):
        for loc in d.get("Location", []):
            out.append((str(d["key"]), loc["path"].rstrip("/")))
    _sections = (time.time(), out)
    return out


def _section_for(path: str, secs: list[tuple[str, str]]) -> tuple[str, str] | None:
    best = None
    for key, loc in secs:
        if path == loc or path.startswith(loc + "/"):
            if best is None or len(loc) > len(best[1]):
                best = (key, loc)
    return best


async def scan(local_dir: str) -> bool:
    """Partial scan of one folder. Returns False if no section covers it."""
    if not enabled():
        return False
    path = plex_path(local_dir).rstrip("/")
    best = _section_for(path, await sections())
    if not best:
        # A library added since the last lookup shouldn't wait out the cache.
        best = _section_for(path, await sections(fresh=True))
    if not best:
        log.warning("Plex scan skipped: no Plex library folder contains %s (add the movies/shows"
                    " symlink folder to a Plex library, or set plex_path_map if Plex sees another path)", path)
        return False
    base = config.get().plex.url.rstrip("/")
    resp = await http.request("GET", f"{base}/library/sections/{best[0]}/refresh",
                              params={"path": path}, headers=_headers())
    return resp.status_code < 400


async def test_connection(url: str, token: str) -> str:
    """'' on success, else an error message (for the setup wizard)."""
    try:
        resp = await http.request("GET", f"{url.rstrip('/')}/identity",
                                  headers={"X-Plex-Token": token, "Accept": "application/json"},
                                  retries=0)
        if resp.status_code == 401:
            return "Plex rejected the token"
        resp.raise_for_status()
        return ""
    except Exception as e:
        return f"Can't reach Plex: {e}"
