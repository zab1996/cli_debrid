"""Item -> Query -> ranked releases.

Episodes are grouped: when two or more episodes of one season are Wanted at
the same time, a season-pack search runs first. If no acceptable pack is
found, each episode is searched on its own.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field, replace

from . import blacklist, config, scrapers
from .db import DB
from .models import Item
from .ranking import Query, Release
from .ranking.score import rank

log = logging.getLogger("clid2.search")


@dataclass
class Job:
    """One search: a movie, one episode, or a season pack for several episodes."""
    query: Query
    items: list[Item]
    results: list[Release] = field(default_factory=list)

    @property
    def label(self) -> str:
        q = self.query
        if q.kind == "season":
            return f"{q.title} S{q.season:02d} (pack, {len(self.items)} eps)"
        return self.items[0].label


def _show_extra(db: DB, tmdb_id: int) -> tuple[list[str], bool, str | None]:
    rows = db.query("SELECT aliases, is_anime, original_title FROM shows WHERE tmdb_id=?", (tmdb_id,))
    if not rows:
        return [], False, None
    r = rows[0]
    return json.loads(r["aliases"] or "[]"), bool(r["is_anime"]), r["original_title"]


def query_for(db: DB, item: Item) -> Query:
    if item.kind == "movie":
        extra = db.query("SELECT value FROM meta_cache WHERE key=?", (f"tmdb:movie:{item.tmdb_id}",))
        aliases = []
        if extra:
            m = json.loads(extra[0]["value"])
            if m.get("original_title") and m["original_title"] != item.title:
                aliases.append(m["original_title"])
        return Query(kind="movie", title=item.title, year=item.year, imdb_id=item.imdb_id,
                     tmdb_id=item.tmdb_id, aliases=aliases)
    aliases, anime, orig = _show_extra(db, item.tmdb_id)
    if orig and orig != item.title:
        aliases = [orig, *aliases]
    return Query(kind="episode", title=item.title, year=item.year, imdb_id=item.imdb_id,
                 tmdb_id=item.tmdb_id, tvdb_id=item.tvdb_id, season=item.season,
                 episode=item.episode, absolute=item.absolute, aliases=aliases, is_anime=anime)


def season_episode_count(db: DB, tmdb_id: int, season: int) -> int:
    rows = db.query("SELECT COUNT(*) n FROM items WHERE kind='episode' AND tmdb_id=? AND season=?",
                    (tmdb_id, season))
    return rows[0]["n"] if rows else 0


def plan(db: DB, items: list[Item]) -> list[Job]:
    """Group Wanted items into search jobs."""
    jobs: list[Job] = []
    by_season: dict[tuple[int, int], list[Item]] = {}
    for it in items:
        if it.kind == "movie":
            jobs.append(Job(query_for(db, it), [it]))
        else:
            by_season.setdefault((it.tmdb_id, it.season), []).append(it)
    # Two or more episodes of one season: search the season once and match
    # single-episode releases (and, with debrid, packs) from that one list.
    for (tmdb_id, season), eps in by_season.items():
        if len(eps) >= 2:
            q = query_for(db, eps[0])
            q.kind = "season"
            q.episode = None
            q.absolute = None
            q.episode_count = season_episode_count(db, tmdb_id, season)
            jobs.append(Job(q, eps))
        else:
            jobs.extend(Job(query_for(db, it), [it]) for it in eps)
    return jobs


def tried_keys(db: DB, items: list[Item]) -> set[str]:
    """Releases to skip: failed/replaced for these items, plus the global release blacklist."""
    blocked = blacklist.release_keys(db)
    if not items:
        return blocked
    marks = ",".join("?" * len(items))
    rows = db.query(f"SELECT DISTINCT release_key FROM releases_tried WHERE item_id IN ({marks})"
                    " AND outcome != 'picked'", tuple(i.id for i in items))
    return {r["release_key"] for r in rows} | blocked


def job_tag(job: Job) -> str:
    if len(job.items) == 1:
        return job.items[0].tag
    return f"[items {','.join(str(i.id) for i in job.items)} {job.label}]"


async def fetch(db: DB, job: Job) -> list[Release]:
    """Ask the indexers once for this job's query (no ranking)."""
    return await scrapers.search_all(job.query)


def rank_copy(db: DB, raw: list[Release], q: Query, items: list[Item]) -> list[Release]:
    """Rank fresh copies of `raw` for one query (ranking mutates releases)."""
    return rank([replace(r, parsed=dict(r.parsed), reject="", score=0.0) for r in raw], q,
                config.get().quality, tried_keys(db, items))


async def run(db: DB, job: Job) -> list[Release]:
    raw = await scrapers.search_all(job.query)
    job.results = rank(raw, job.query, config.get().quality, tried_keys(db, job.items))
    reasons = Counter(r.reject.split(" (")[0] for r in raw if r.reject)
    log.info("%s %d results, %d acceptable; rejected: %s", job_tag(job), len(raw), len(job.results),
             ", ".join(f"{k}={v}" for k, v in reasons.most_common(8)) or "none")
    for n, r in enumerate(job.results[:3], 1):
        log.info("%s   #%d score %.1f %s %.1fGB %s%s: %s", job_tag(job), n, r.score, r.protocol,
                 r.size_gb, r.indexer, " cached" if r.cached else "", r.title)
    return job.results


def split(db: DB, job: Job) -> list[Job]:
    """A season job that found nothing becomes one job per episode."""
    return [Job(query_for(db, it), [it]) for it in job.items]
