"""Manual search: show every release for a title and grab the one you pick.

Used by Discover, the Library and the item/show pages. A search is kept in
memory for a while so the "Grab" click can refer to it by id + row number.
Picking for something already in the library replaces it (the old file keeps
playing until the new one is ready).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from . import acquire, blacklist, catalog, config, scrapers
from .db import DB
from .downloads import engine
from .metadata import tmdb
from .models import Item, State
from .ranking import Query, Release
from .ranking.score import rank

log = logging.getLogger("clid2.interactive")

KEEP_SECONDS = 30 * 60


@dataclass
class Search:
    id: str
    kind: str                     # movie | episode | season
    tmdb_id: int
    season: int | None
    episode: int | None
    label: str
    query: Query
    releases: list[Release] = field(default_factory=list)   # accepted first, then rejected
    took: float = 0.0
    at: float = field(default_factory=time.time)


_searches: dict[str, Search] = {}


def _forget_old() -> None:
    cutoff = time.time() - KEEP_SECONDS
    for k in [k for k, s in _searches.items() if s.at < cutoff]:
        del _searches[k]


def get(search_id: str) -> Search | None:
    return _searches.get(search_id)


async def _query(db: DB, kind: str, tmdb_id: int, season: int | None, episode: int | None) -> tuple[Query, str]:
    if kind == "movie":
        m = await tmdb.movie(db, tmdb_id)
        aliases = [m["original_title"]] if m.get("original_title") and m["original_title"] != m["title"] else []
        q = Query(kind="movie", title=m["title"], year=m["year"], imdb_id=m["imdb_id"],
                  tmdb_id=tmdb_id, aliases=aliases, runtime=m.get("runtime") or 0)
        return q, f"{m['title']} ({m['year']})"
    info = await tmdb.show(db, tmdb_id)
    aliases = list(info.get("aliases") or [])
    if info.get("original_title") and info["original_title"] != info["title"]:
        aliases.insert(0, info["original_title"])
    absolute = None
    if kind == "episode":
        rows = db.query("SELECT absolute FROM items WHERE kind='episode' AND tmdb_id=? AND season=?"
                        " AND episode=?", (tmdb_id, season, episode))
        absolute = rows[0]["absolute"] if rows else None
    count = len((info.get("seasons") or {}).get(str(season), [])) if season else 1
    q = Query(kind=kind, title=info["title"], year=info["year"], imdb_id=info["imdb_id"],
              tmdb_id=tmdb_id, tvdb_id=info.get("tvdb_id"), season=season, episode=episode,
              absolute=absolute, aliases=aliases, is_anime=bool(info.get("is_anime")),
              episode_count=max(count, 1), runtime=info.get("runtime") or 0)
    label = f"{info['title']} S{season:02d}" + (f"E{episode:02d}" if kind == "episode" else " (season pack)")
    return q, label


async def run(db: DB, kind: str, tmdb_id: int, season: int | None = None,
              episode: int | None = None) -> Search:
    _forget_old()
    q, label = await _query(db, kind, tmdb_id, season, episode)
    started = time.time()
    raw = await scrapers.search_all(q)
    # Only the global release blacklist is excluded up front; releases that
    # failed before are still shown (marked) so you can choose them anyway.
    accepted = rank(raw, q, config.get().quality, blacklist.release_keys(db))
    hashes = [r.info_hash for r in raw if r.protocol == "torrent" and r.info_hash][:200]
    if hashes:
        try:
            cached = await engine.cached(hashes)
            for r in raw:
                if r.protocol == "torrent" and cached.get(r.info_hash):
                    r.cached = True
        except engine.EngineError:
            pass
    seen = {r.key for r in accepted}
    rejected, dup = [], set()
    for r in raw:
        if r.key in seen or r.key in dup or not r.reject:
            continue
        dup.add(r.key)
        rejected.append(r)
    rejected.sort(key=lambda r: r.size_gb, reverse=True)
    s = Search(id=uuid.uuid4().hex[:12], kind=kind, tmdb_id=tmdb_id, season=season,
               episode=episode, label=label, query=q, releases=accepted + rejected,
               took=time.time() - started)
    _searches[s.id] = s
    log.info("[manual %s] %s: %d results, %d acceptable, %d rejected (%.1fs)", s.id, label,
             len(raw), len(accepted), len(rejected), s.took)
    return s


def previous_outcomes(db: DB, s: Search) -> dict[str, str]:
    """release key -> 'failed'/'picked'/... for the items this search is about."""
    ids = [it.id for it in _existing_items(db, s)]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["release_key"]: r["outcome"] for r in db.query(
        f"SELECT release_key, outcome FROM releases_tried WHERE item_id IN ({marks})", tuple(ids))}


def _existing_items(db: DB, s: Search) -> list[Item]:
    if s.kind == "movie":
        rows = db.query("SELECT id FROM items WHERE kind='movie' AND tmdb_id=?", (s.tmdb_id,))
    elif s.kind == "episode":
        rows = db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=? AND season=? AND episode=?",
                        (s.tmdb_id, s.season, s.episode))
    else:
        rows = db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=? AND season=?",
                        (s.tmdb_id, s.season))
    return [it for it in (db.get_item(r["id"]) for r in rows) if it]


def to_json(db: DB, s: Search) -> dict:
    before = previous_outcomes(db, s)
    rows = []
    for i, r in enumerate(s.releases):
        p = r.parsed or {}
        rows.append({
            "i": i, "title": r.title, "protocol": r.protocol, "indexer": r.indexer,
            "size_gb": r.size_gb, "seeders": r.seeders, "score": r.score if not r.reject else None,
            "reject": "blocked release" if r.reject == "already tried" else r.reject,
            "cached": bool(r.cached), "before": before.get(r.key, ""),
            "current": before.get(r.key) == "picked",
            "resolution": p.get("resolution") or "", "source": p.get("quality") or "",
            "codec": (p.get("codec") or "").upper(), "hdr": ", ".join(p.get("hdr") or []),
            "audio": ", ".join((p.get("audio") or [])[:2]),
            "languages": ", ".join(p.get("languages") or []),
            "grabbable": not r.reject.startswith("usenet pack"),
            "version": p.get("_version", ""),
        })
    return {"id": s.id, "label": s.label, "kind": s.kind, "took": round(s.took, 1),
            "accepted": sum(1 for r in s.releases if not r.reject), "results": rows}


async def _ensure_items(db: DB, s: Search) -> list[Item]:
    """Make sure the movie/episodes exist in the library; returns the targets."""
    items = _existing_items(db, s)
    if not items:
        if s.kind == "movie":
            await catalog.add_movie(db, s.tmdb_id)
        else:
            await catalog.add_show(db, s.tmdb_id, seasons=[s.season])
        items = _existing_items(db, s)
    return items


async def grab(db: DB, search_id: str, index: int) -> tuple[bool, str, int | None]:
    """Hand the chosen release to the engine. Returns (ok, message, item id to show)."""
    s = get(search_id)
    if not s:
        return False, "This search has expired; search again.", None
    if not 0 <= index < len(s.releases):
        return False, "Unknown release.", None
    r = s.releases[index]
    if r.reject.startswith("usenet pack"):
        return False, "Usenet season packs are never used.", None
    try:
        items = await _ensure_items(db, s)
    except catalog.Blacklisted as e:
        return False, f"{e}. Remove it on the Blacklist page first.", None
    if not items:
        return False, "Nothing to grab for (no matching episodes).", None
    # Leave anything that's mid-download alone (a pack covers the rest of the season).
    items = [it for it in items if it.state is not State.DOWNLOADING]
    if not items:
        return False, "Already downloading; wait for it to finish or fail first.", None
    for it in items:
        if it.state is State.DONE:
            prev = db.query("SELECT release_key, name FROM releases_tried WHERE item_id=? AND outcome='picked'",
                            (it.id,))
            for p in prev:
                if p["release_key"] != r.key:
                    db.record_release(it.id, p["release_key"], p["name"], None, "replaced")
    fresh = [db.get_item(it.id) for it in items]
    log.info("[manual %s] grabbing #%d for %d item(s): %s", s.id, index, len(fresh), r.title)
    try:
        await acquire.hand_over(db, fresh, r, label=s.label)
    except engine.EngineDown:
        return False, "The mount engine isn't running.", None
    except Exception as e:
        for it in fresh:
            db.record_release(it.id, r.key, r.title, r.score, "failed")
        log.info("[manual %s] refused: %s: %s", s.id, type(e).__name__, e)
        return False, f"Refused: {e}", fresh[0].id if len(fresh) == 1 else None
    return True, f"Grabbed for {s.label}", fresh[0].id if len(fresh) == 1 else None


def params_for_item(it: Item) -> dict:
    if it.kind == "movie":
        return {"kind": "movie", "tmdb_id": it.tmdb_id}
    return {"kind": "episode", "tmdb_id": it.tmdb_id, "season": it.season, "episode": it.episode}
