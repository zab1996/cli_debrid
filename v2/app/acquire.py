"""Searching -> Downloading -> Done.

Picking: rank results, mark which torrents are cached, then hand the best
candidates to the engine one by one until one is accepted. Uncached torrents
are rejected by the engine, so a rejection simply means "try the next one".

Replacing (upgrade or Plex report): the item keeps its current file while the
new release downloads. When the new one is ready the symlink is repointed and
the old download deleted; if it fails, an upgrade quietly returns to Done.
"""
from __future__ import annotations

import logging
import time

from . import config, http, notify, search
from .db import DB
from .downloads import engine
from .library import match, plex, symlink
from .models import MAX_EMPTY_SEARCHES, Item, State, next_backoff
from .ranking import Release
from .ranking.score import CACHED_BONUS
from .scrapers import magnet_for

log = logging.getLogger("clid2.acquire")

SEARCH_BATCH = 10          # Wanted items handled per tick
CANDIDATES = 5             # releases tried per search before giving up for now
SWEEP_FALLBACK = 5         # per season search: episodes that may get their own search
UPGRADE_MARGIN = 5.0       # an upgrade must beat the current score by this much
TIMEOUT = {"torrent": 6 * 3600, "nzb": 2 * 3600}
VANISH_GRACE = 300         # an engine id that never appears is failed after this


# ------------------------------------------------------------------ search


async def _mark_cached(results: list[Release], q) -> None:
    hashes = [r.info_hash for r in results[:40] if r.protocol == "torrent"]
    if not hashes:
        return
    try:
        cached = await engine.cached(hashes)
    except engine.EngineError as e:
        log.debug("cache check failed: %s", e)
        return
    for r in results:
        if r.protocol == "torrent" and cached.get(r.info_hash) and not r.cached:
            r.cached = True
            r.score += CACHED_BONUS
    results.sort(key=lambda r: r.score, reverse=True)


async def _fetch_nzb(url: str) -> bytes:
    resp = await http.request("GET", url, timeout=60)
    if resp.status_code >= 400:
        raise http.HTTPError(resp.status_code, url, resp.text)
    body = resp.content
    if b"<nzb" not in body[:4096].lower() and b"<?xml" not in body[:100].lower():
        raise ValueError("indexer did not return an NZB")
    return body


async def _submit(r: Release) -> str:
    if r.protocol == "torrent":
        return await engine.add_magnet(r.magnet or magnet_for(r.info_hash, r.title))
    return await engine.add_nzb(await _fetch_nzb(r.nzb_url), r.title)


def current_pick(db: DB, item: Item) -> tuple[str, float | None] | None:
    """(release key, score) of the release currently in use."""
    rows = db.query("SELECT release_key, score FROM releases_tried WHERE item_id=? AND"
                    " outcome='picked' ORDER BY at DESC LIMIT 1", (item.id,))
    return (rows[0]["release_key"], rows[0]["score"]) if rows else None


async def _try_candidates(db: DB, items: list[Item], results: list[Release], tag: str,
                          label: str, upgrade: bool = False) -> bool:
    for n, r in enumerate(results[:CANDIDATES], 1):
        log.info("%s trying #%d (%s, %.1fGB, score %.1f, %s): %s", tag, n, r.protocol, r.size_gb,
                 r.score, r.indexer, r.title)
        try:
            await hand_over(db, items, r, label=label, upgrade=upgrade)
        except engine.EngineDown:
            raise
        except http.RateLimited as e:
            # Our own indexer cap, not the release's fault: try again later.
            log.info("%s   not fetched yet: %s", tag, e)
            return False
        except Exception as e:
            log.info("%s   rejected: %s: %s", tag, type(e).__name__, e)
            for it in items:
                db.record_release(it.id, r.key, r.title, r.score, "failed")
            continue
        return True
    return False


async def _run_season(db: DB, job: search.Job) -> bool:
    """One search for a whole season, shared by every wanted episode in it.

    1. Ask the indexers once for the season (ID search pages through results).
    2. With debrid, a season pack covering everything is tried first.
    3. Each episode then picks its best single-episode release from that list.
    4. Episodes still without a match get their own search, at most
       SWEEP_FALLBACK per sweep; the rest wait for the next round.
    """
    tag = search.job_tag(job)
    raw = await search.fetch(db, job)
    cfg = config.get()
    if cfg.debrid.provider and cfg.debrid.api_key:
        packs = search.rank_copy(db, raw, job.query, job.items)
        if packs:
            await _mark_cached(packs, job.query)
            if await _try_candidates(db, job.items, packs, tag, job.label):
                return True
    matched, leftovers = 0, []
    for it in job.items:
        q = search.query_for(db, it)
        cands = search.rank_copy(db, raw, q, [it])
        if cands:
            await _mark_cached(cands, q)
            if await _try_candidates(db, [it], cands, it.tag, it.label):
                matched += 1
                continue
        leftovers.append(it)
    log.info("%s season search (%d results) covered %d of %d episodes; %d left", tag, len(raw),
             matched, len(job.items), len(leftovers))
    for it in leftovers[:SWEEP_FALLBACK]:
        if await run_job(db, search.Job(search.query_for(db, it), [it])):
            matched += 1
    for it in leftovers[SWEEP_FALLBACK:]:
        _nothing_found(db, db.get_item(it.id) or it)
    return matched > 0


async def run_job(db: DB, job: search.Job, upgrade: bool = False) -> bool:
    """Search and submit. Returns True if something was handed to the engine."""
    if job.query.kind == "season" and not upgrade:
        return await _run_season(db, job)
    results = await search.run(db, job)
    if results:
        await _mark_cached(results, job.query)
    if upgrade:
        # Compare against the current release scored the same way, today: a
        # score saved earlier may come from an older formula or version.
        cur = current_pick(db, job.items[0])
        if not cur:
            return False
        current = next((r for r in results if r.key == cur[0]), None)
        if current is None:
            # Not in today's results (or no longer passes the version): leave it.
            log.debug("%s upgrade check: current release not in results; skipped", job.items[0].tag)
            return False
        better = [r for r in results if r.key != cur[0] and r.score >= current.score + UPGRADE_MARGIN]
        if not better:
            return False
        log.info("%s upgrade: %.1f -> %.1f", job.items[0].tag, current.score, better[0].score)
        results = better[:1]  # only the clearly better one
    tag = search.job_tag(job)
    if await _try_candidates(db, job.items, results, tag, job.label, upgrade):
        return True
    if results:
        log.info("%s none of the top %d candidates were accepted", tag, min(len(results), CANDIDATES))
    if not upgrade:
        for it in job.items:
            _nothing_found(db, it)
    return False


async def hand_over(db: DB, items: list[Item], r: Release, label: str = "",
                    upgrade: bool = False) -> str:
    """Give one release to the engine for these items. Raises if it's refused.

    Items already in the library keep their current file until the new one is
    ready (mode 'replace', or 'upgrade' for the upgrade sweep).
    """
    engine_id = await _submit(r)
    now = time.time()
    for it in items:
        db.record_release(it.id, r.key, r.title, r.score, "picked")
        fields = dict(engine_id=engine_id, release_name=r.title, downloading_since=now)
        if it.state is State.DONE or it.mode:
            fields["prev_engine_id"] = it.engine_id if it.engine_id != engine_id else None
            fields["mode"] = it.mode or ("upgrade" if upgrade else "replace")
        db.set_state(it.id, State.DOWNLOADING, **fields)
    label = label or (items[0].label if len(items) == 1 else f"{len(items)} items")
    db.log_event(f"{'Upgrading' if upgrade else 'Grabbed'} {label}: {r.title}",
                 items[0].id if len(items) == 1 else None)
    tag = items[0].tag if len(items) == 1 else f"[items {','.join(str(i.id) for i in items)}]"
    log.info("%s   accepted by engine as %s%s", tag, engine_id, " (upgrade)" if upgrade else "")
    return engine_id


def _nothing_found(db: DB, it: Item) -> None:
    if it.mode == "replace" and it.file_path:
        # Keep serving the old (reported) file until something better shows up.
        db.set_state(it.id, State.DONE, mode=None, next_attempt_at=None)
        db.log_event(f"No replacement found for {it.label}; keeping current file", it.id)
        return
    attempts = it.attempts + 1
    if attempts >= MAX_EMPTY_SEARCHES:
        db.set_state(it.id, State.FAILED, attempts=attempts, next_attempt_at=None)
        db.log_event(f"Gave up on {it.label} after {attempts} searches", it.id)
        notify.emit(db, "failed", it.label, f"nothing usable after {attempts} searches", it.id)
        log.warning("%s nothing usable after %d searches -> Failed (weekly retry)", it.tag, attempts)
    else:
        wait = next_backoff(attempts)
        db.set_state(it.id, State.WAITING, attempts=attempts, next_attempt_at=time.time() + wait)
        log.info("%s nothing usable (search %d/%d) -> Waiting %dh", it.tag, attempts,
                 MAX_EMPTY_SEARCHES, wait // 3600)


async def search_wanted(db: DB) -> None:
    items = db.items_in(State.WANTED, limit=SEARCH_BATCH)
    if not items:
        return
    try:
        await engine.status()
    except engine.EngineError:
        return  # nothing could be handed over; don't burn searches
    for it in items:
        db.set_state(it.id, State.SEARCHING)
    items = [db.get_item(i.id) for i in items]
    for job in search.plan(db, items):
        try:
            await run_job(db, job)
        except engine.EngineDown:
            log.warning("engine went away during %s; will retry", job.label)
            for it in job.items:
                if db.get_item(it.id).state is State.SEARCHING:
                    db.set_state(it.id, State.WANTED)
            return
        except Exception:
            log.exception("search failed for %s", job.label)
            for it in job.items:
                db.set_state(it.id, State.WANTED)


def recover_interrupted(db: DB) -> None:
    """At startup: anything left in Searching was interrupted; search it again."""
    for it in db.items_in(State.SEARCHING, limit=100000):
        db.set_state(it.id, State.WANTED)


# ------------------------------------------------------------------ downloads


async def _place(db: DB, it: Item, path: str) -> str:
    cfg = config.get()
    if cfg.library.mode == "symlink":
        link = symlink.place(it, cfg.library, path)
        if it.file_path and it.file_path != str(link):
            symlink.remove(it.file_path)
        return str(link)
    return path


async def _finish(db: DB, items: list[Item], st: engine.EntryStatus) -> None:
    if items[0].kind == "movie":
        f = match.for_movie(st.files)
        found = {items[0].id: f} if f else {}
    else:
        found = match.for_episodes(st.files, items)
    scan_dirs: set[str] = set()
    old_ids: set[str] = set()
    log.info("[download %s] ready: %s (%d files, %d videos)", st.id, st.name, len(st.files),
             len(match.videos(st.files)))
    for it in items:
        f = found.get(it.id)
        if not f:
            log.warning("%s no matching file in %s; files: %s", it.tag, st.name,
                        ", ".join(x.name for x in match.videos(st.files)[:8]) or "none")
            db.record_release(it.id, st.id, st.name, None, "failed")
            _back_to_search(db, it, f"{st.name} has no file for it")
            continue
        try:
            path = await _place(db, it, f.path)
        except OSError as e:
            log.error("%s placing %s failed: %s", it.tag, f.path, e)
            _back_to_search(db, it, f"could not create library link ({e})")
            continue
        if it.prev_engine_id:
            old_ids.add(it.prev_engine_id)
        if it.mode:
            notify.emit(db, "upgraded", it.label, f.name.rsplit("/", 1)[-1], it.id)
        else:
            notify.emit(db, "collected", it.label, st.name, it.id)
        db.set_state(it.id, State.DONE, file_path=path, done_at=time.time(), mode=None,
                     prev_engine_id=None, attempts=0, downloading_since=None)
        log.info("%s Done: %s -> %s", it.tag, f.name, path)
        scan_dirs.add(str(symlink.folder_for(it, config.get().library))
                      if config.get().library.mode == "symlink" else path.rsplit("/", 1)[0])
    done = [it for it in items if it.id in found]
    if done:
        db.log_event(f"Ready: {done[0].label}" + (f" (+{len(done) - 1} more)" if len(done) > 1 else ""))
    # Nothing references the old download any more: remove it.
    for old in old_ids:
        if not db.engine_id_in_use(old):
            try:
                await engine.delete(old)
            except engine.EngineError as e:
                log.warning("could not delete old download %s: %s", old, e)
    if not db.engine_id_in_use(st.id):
        await engine.delete(st.id)
    for d in scan_dirs:
        try:
            ok = await plex.scan(d)
            log.info("Plex scan %s: %s", "requested" if ok else "skipped", d)
        except Exception as e:
            log.warning("Plex scan of %s failed: %s", d, e)


def _back_to_search(db: DB, it: Item, why: str) -> None:
    if it.mode == "upgrade" and it.file_path:
        db.set_state(it.id, State.DONE, engine_id=it.prev_engine_id or it.engine_id,
                     prev_engine_id=None, mode=None, downloading_since=None)
        return
    # A replacement keeps pointing at the old download (still in the library)
    # until a new one succeeds; a first download starts from scratch.
    keep_old = it.mode == "replace" and it.file_path
    db.set_state(it.id, State.WANTED, engine_id=it.prev_engine_id if keep_old else None,
                 prev_engine_id=None, downloading_since=None)
    db.log_event(f"{it.label}: {why}; searching again", it.id)


async def _fail(db: DB, items: list[Item], engine_id: str, why: str) -> None:
    for it in items:
        log.warning("%s download %s failed: %s", it.tag, engine_id, why)
        if it.release_name:
            db.record_release(it.id, engine_id.lower(), it.release_name, None, "failed")
        _back_to_search(db, it, why)
    if not db.engine_id_in_use(engine_id):
        try:
            await engine.delete(engine_id)
        except engine.EngineError:
            pass


async def check_downloads(db: DB) -> None:
    groups: dict[str, list[Item]] = {}
    for it in db.items_in(State.DOWNLOADING, limit=500):
        if it.engine_id:
            groups.setdefault(it.engine_id, []).append(it)
        else:
            db.set_state(it.id, State.WANTED)
    now = time.time()
    for engine_id, items in groups.items():
        since = items[0].downloading_since or now
        try:
            st = await engine.entry(engine_id)
        except engine.EngineError as e:
            log.debug("engine unavailable: %s", e)
            return  # try again next tick
        if st is None:
            if now - since > VANISH_GRACE:
                await _fail(db, items, engine_id, "download disappeared")
            continue
        if st.status != "ready":
            log.debug("[download %s] %s %.0f%%", engine_id, st.status, st.progress)
        if st.status == "ready":
            await _finish(db, items, st)
        elif st.status == "error":
            await _fail(db, items, engine_id, f"download failed ({st.error or 'error'})")
        elif now - since > TIMEOUT.get(st.protocol, TIMEOUT["torrent"]):
            await _fail(db, items, engine_id, "download timed out")
