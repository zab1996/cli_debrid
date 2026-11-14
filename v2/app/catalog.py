"""Turning "I want X" into item rows, and keeping show episodes up to date."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import blacklist, config
from .db import DB
from .metadata import cache, tmdb, tvdb
from .models import State

log = logging.getLogger("clid2.catalog")

# Releases rarely appear before the episode has aired somewhere; start looking
# this many hours after the start of the air date (UTC).
EPISODE_DELAY_HOURS = 20


def release_ts(release_date: str | None, kind: str) -> float | None:
    """When an item becomes searchable, as a unix timestamp. None = unknown."""
    if not release_date:
        return None
    try:
        d = datetime.fromisoformat(release_date[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ts = d.timestamp()
    if kind == "episode":
        ts += config.get().adv("episode_delay_hours", EPISODE_DELAY_HOURS) * 3600
    return ts


def initial_state(release_date: str | None, kind: str) -> tuple[State, float | None]:
    ts = release_ts(release_date, kind)
    if ts is None:
        # Movies without any date: just try. Episodes without a date haven't
        # been scheduled yet; check again tomorrow.
        if kind == "movie":
            return State.WANTED, None
        return State.WAITING, time.time() + 24 * 3600
    if ts > time.time():
        return State.WAITING, ts
    return State.WANTED, None


class Blacklisted(Exception):
    pass


async def add_movie(db: DB, tmdb_id: int, source: str = "manual") -> int | None:
    if blacklist.is_blocked(db, "movie", tmdb_id):
        log.info("[movie tmdb %s] not added: blacklisted (source %s)", tmdb_id, source)
        if source == "manual":
            raise Blacklisted("This movie is blacklisted")
        return None
    m = await tmdb.movie(db, tmdb_id)
    state, nxt = initial_state(m["release_date"], "movie")
    item_id = db.add_item(kind="movie", tmdb_id=tmdb_id, imdb_id=m["imdb_id"], title=m["title"],
                          year=m["year"], release_date=m["release_date"], source=source,
                          state=state, next_attempt_at=nxt, poster=m.get("poster"),
                          runtime=m.get("runtime") or None)
    if item_id:
        db.log_event(f"Added movie {m['title']} ({m['year']})", item_id)
        log.info("[item %s %s (%s)] added movie: tmdb %s imdb %s, home release %s -> %s%s, source %s",
                 item_id, m["title"], m["year"], tmdb_id, m["imdb_id"], m["release_date"] or "unknown",
                 state.value, f" until {time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt))}" if nxt else "",
                 source)
        if not m["imdb_id"]:
            log.warning("[item %s %s] has no IMDb id: Torrentio and IMDb-based indexer searches "
                        "will find nothing", item_id, m["title"])
    else:
        log.info("[movie tmdb %s %s] already in the library (source %s)", tmdb_id, m["title"], source)
    return item_id


async def _episodes_for(db: DB, info: dict) -> dict[str, list[dict]]:
    """TVDB aired order when available, else TMDB."""
    if tvdb.enabled() and info.get("tvdb_id"):
        try:
            eps = await tvdb.episodes(db, int(info["tvdb_id"]))
            if eps:
                return eps
            log.info("[show tmdb %s %s] TVDB returned no episodes; using TMDB",
                     info["tmdb_id"], info["title"])
        except Exception as e:
            log.warning("[show tmdb %s %s] TVDB episodes failed, using TMDB: %s",
                        info["tmdb_id"], info["title"], e)
    return info["seasons"]


async def add_show(db: DB, tmdb_id: int, seasons: list[int] | None = None,
                   source: str = "manual") -> int:
    """Request a show (all seasons, or only `seasons`). Returns episodes added."""
    if blacklist.is_blocked(db, "show", tmdb_id):
        log.info("[show tmdb %s] not added: blacklisted (source %s)", tmdb_id, source)
        if source == "manual":
            raise Blacklisted("This show is blacklisted")
        return 0
    info = await tmdb.show(db, tmdb_id)
    now = time.time()
    with db.tx() as c:
        if source == "manual":
            # Adding by hand means "I want it": bring back episodes deleted earlier.
            if seasons:
                marks = ",".join("?" * len(seasons))
                c.execute(f"DELETE FROM skipped_episodes WHERE tmdb_id=? AND season IN ({marks})",
                          (tmdb_id, *seasons))
            else:
                c.execute("DELETE FROM skipped_episodes WHERE tmdb_id=?", (tmdb_id,))
        row = c.execute("SELECT seasons FROM shows WHERE tmdb_id=?", (tmdb_id,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO shows(tmdb_id, tvdb_id, imdb_id, title, original_title, year, aliases,"
                " is_anime, status, seasons, source, added_at, poster, runtime)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tmdb_id, info["tvdb_id"], info["imdb_id"], info["title"], info["original_title"],
                 info["year"], json.dumps(info["aliases"]), int(info["is_anime"]), info["status"],
                 json.dumps(seasons) if seasons else None, source, now, info.get("poster"),
                 info.get("runtime") or None),
            )
        elif row["seasons"] is not None:
            # Widen an existing partial request.
            merged = None if seasons is None else sorted(set(json.loads(row["seasons"])) | set(seasons))
            c.execute("UPDATE shows SET seasons=? WHERE tmdb_id=?",
                      (json.dumps(merged) if merged else None, tmdb_id))
    added = await sync_show(db, tmdb_id)
    log.info("[show tmdb %s %s] requested %s from %s: %d new episodes (tvdb %s, imdb %s)",
             tmdb_id, info["title"], f"seasons {seasons}" if seasons else "all seasons", source,
             added, info["tvdb_id"], info["imdb_id"])
    if added:
        db.log_event(f"Added {info['title']}: {added} episodes")
    return added


async def sync_show(db: DB, tmdb_id: int) -> int:
    """Add newly announced episodes and refresh air dates of unfinished ones."""
    rows = db.query("SELECT * FROM shows WHERE tmdb_id=?", (tmdb_id,))
    if not rows:
        return 0
    show = rows[0]
    wanted_seasons = set(json.loads(show["seasons"])) if show["seasons"] else None
    info = await tmdb.show(db, tmdb_id)
    episodes = await _episodes_for(db, info)
    added = 0
    skipped_blocked = skipped_deleted = 0
    for sn_str, eps in episodes.items():
        sn = int(sn_str)
        if sn <= 0 or (wanted_seasons is not None and sn not in wanted_seasons):
            continue
        if blacklist.is_blocked(db, "show", tmdb_id, sn):
            skipped_blocked += len(eps)
            continue
        skipped = {r["episode"] for r in db.query(
            "SELECT episode FROM skipped_episodes WHERE tmdb_id=? AND season=?", (tmdb_id, sn))}
        for e in eps:
            if e["episode"] in skipped:
                skipped_deleted += 1
                continue
            state, nxt = initial_state(e.get("air_date"), "episode")
            new_id = db.add_item(
                kind="episode", tmdb_id=tmdb_id, tvdb_id=info["tvdb_id"], imdb_id=info["imdb_id"],
                title=info["title"], year=info["year"], season=sn, episode=e["episode"],
                absolute=e.get("absolute"), release_date=e.get("air_date"),
                source=show["source"], state=state, next_attempt_at=nxt,
                runtime=e.get("runtime") or info.get("runtime") or None,
            )
            if new_id:
                added += 1
            else:
                _refresh_waiting_episode(db, tmdb_id, sn, e)
    with db.tx() as c:
        c.execute("UPDATE shows SET refreshed_at=?, status=? WHERE tmdb_id=?",
                  (time.time(), info["status"], tmdb_id))
    if added or skipped_blocked or skipped_deleted:
        log.info("[show tmdb %s %s] sync: %d added, %d skipped (blacklisted season), "
                 "%d skipped (deleted by you)", tmdb_id, info["title"], added, skipped_blocked,
                 skipped_deleted)
    return added


def _refresh_waiting_episode(db: DB, tmdb_id: int, season: int, e: dict) -> None:
    rows = db.query("SELECT id, state, release_date, absolute FROM items WHERE kind='episode'"
                    " AND tmdb_id=? AND season=? AND episode=?", (tmdb_id, season, e["episode"]))
    if not rows:
        return
    r = rows[0]
    fields = {}
    if e.get("absolute") and r["absolute"] != e["absolute"]:
        fields["absolute"] = e["absolute"]
    if e.get("air_date") and e["air_date"] != r["release_date"]:
        fields["release_date"] = e["air_date"]
        if r["state"] == State.WAITING.value:
            state, nxt = initial_state(e["air_date"], "episode")
            db.set_state(r["id"], state, next_attempt_at=nxt, **fields)
            return
    if fields:
        db.set_state(r["id"], State(r["state"]), **fields)


async def backfill_posters(db: DB, limit: int = 25) -> None:
    """Fill in posters/runtimes for titles added before those were stored."""
    for r in db.query("SELECT DISTINCT kind, tmdb_id FROM items WHERE runtime IS NULL LIMIT ?", (limit,)):
        try:
            if r["kind"] == "movie":
                m = await tmdb.movie(db, r["tmdb_id"])
                rt = m.get("runtime") or 0
            else:
                info = await tmdb.show(db, r["tmdb_id"])
                if "runtime" not in info:
                    cache.drop(db, f"tmdb:show:{r['tmdb_id']}")
                    info = await tmdb.show(db, r["tmdb_id"])
                rt = info.get("runtime") or 0
        except Exception:
            continue
        with db.tx() as c:
            c.execute("UPDATE items SET runtime=? WHERE kind=? AND tmdb_id=? AND runtime IS NULL",
                      (rt, r["kind"], r["tmdb_id"]))
    for r in db.query("SELECT DISTINCT tmdb_id FROM items WHERE kind='movie' AND poster IS NULL"
                      " LIMIT ?", (limit,)):
        try:
            m = await tmdb.movie(db, r["tmdb_id"])
            if "poster" not in m:  # cached before posters were stored
                cache.drop(db, f"tmdb:movie:{r['tmdb_id']}")
                m = await tmdb.movie(db, r["tmdb_id"])
        except Exception:
            continue
        with db.tx() as c:
            c.execute("UPDATE items SET poster=? WHERE kind='movie' AND tmdb_id=?",
                      (m.get("poster") or "", r["tmdb_id"]))
    for r in db.query("SELECT tmdb_id FROM shows WHERE poster IS NULL LIMIT ?", (limit,)):
        try:
            info = await tmdb.show(db, r["tmdb_id"])
            if "poster" not in info:
                cache.drop(db, f"tmdb:show:{r['tmdb_id']}")
                info = await tmdb.show(db, r["tmdb_id"])
        except Exception:
            continue
        with db.tx() as c:
            c.execute("UPDATE shows SET poster=? WHERE tmdb_id=?", (info.get("poster") or "", r["tmdb_id"]))


async def refresh_all_shows(db: DB) -> None:
    """Daily: pick up new episodes for shows that aren't finished."""
    for r in db.query("SELECT tmdb_id, title, status FROM shows"):
        if r["status"] in ("Ended", "Canceled"):
            # Still refresh ended shows weekly-ish via the cache TTL; skip most days.
            if int(time.time() // 86400) % 7 != r["tmdb_id"] % 7:
                continue
        try:
            n = await sync_show(db, r["tmdb_id"])
            if n:
                db.log_event(f"{r['title']}: {n} new episodes")
        except Exception as e:
            log.warning("refresh %s failed: %s", r["title"], e)
