import asyncio
import hashlib
import os
import time

import pytest

from app import acquire, config, scrapers
from app.downloads import engine
from app.downloads.engine import EngineFile, EntryStatus
from app.models import State
from app.ranking import Release
from app.repair import plex_issues

GB = 1024 ** 3


def run(c):
    return asyncio.run(c)


class FakeEngine:
    def __init__(self, tmp):
        self.tmp = tmp
        self.entries: dict[str, EntryStatus] = {}
        self.reject: set[str] = set()
        self.deleted: list[str] = []
        self.n = 0

    down = False

    async def status(self):
        if self.down:
            raise engine.EngineDown("down")
        return {"ready": True}

    async def cached(self, hashes):
        return {h: h.startswith("c") for h in hashes}

    async def add_magnet(self, magnet):
        h = scrapers.hash_from_magnet(magnet)
        if h in self.reject:
            raise engine.EngineError("not cached")
        self.entries[h] = EntryStatus(id=h, name=h, protocol="torrent", status="downloading")
        return h

    def ready(self, eid, names):
        files = []
        for n in names:
            p = self.tmp / "mount" / eid / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
            files.append(EngineFile(n, 2 * GB, str(p)))
        self.entries[eid].status = "ready"
        self.entries[eid].files = files

    async def entry(self, eid):
        return self.entries.get(eid)

    async def delete(self, eid):
        self.deleted.append(eid)
        self.entries.pop(eid, None)


@pytest.fixture
def env(tmp_path, monkeypatch, db):
    cfg = config.Config()
    cfg.library.movies_path = str(tmp_path / "movies")
    cfg.library.shows_path = str(tmp_path / "shows")
    monkeypatch.setattr(config, "_current", cfg)
    fe = FakeEngine(tmp_path)
    for name in ("cached", "add_magnet", "entry", "delete", "status"):
        monkeypatch.setattr(engine, name, getattr(fe, name))
    results: list[Release] = []

    async def search_all(q):
        return [Release(**r.__dict__) for r in results]
    monkeypatch.setattr(scrapers, "search_all", search_all)

    async def no_scan(d):
        return False
    monkeypatch.setattr(acquire.plex, "scan", no_scan)
    return fe, results, tmp_path


def H(name):
    """Readable test id -> valid 40-hex info hash."""
    return hashlib.sha1(name.encode()).hexdigest()


def T(title, h, size=4.0):
    return Release(title=title, protocol="torrent", indexer="t", size_gb=size, info_hash=H(h))


def test_movie_happy_path_with_rejected_first(env, db):
    fe, results, tmp = env
    results += [T("The.Matrix.1999.1080p.BluRay.x264", "a1", 10),
                T("The.Matrix.1999.1080p.WEB-DL", "b2", 4)]
    fe.reject.add(H("a1"))
    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    run(acquire.search_wanted(db))
    it = db.get_item(mid)
    assert it.state is State.DOWNLOADING and it.engine_id == H("b2")
    outcomes = {r["release_key"]: r["outcome"] for r in db.query("SELECT * FROM releases_tried")}
    assert outcomes == {H("a1"): "failed", H("b2"): "picked"}

    run(acquire.check_downloads(db))
    assert db.get_item(mid).state is State.DOWNLOADING
    fe.ready(H("b2"), ["The.Matrix.1999.1080p.WEB-DL.mkv", "sample.mkv"])
    run(acquire.check_downloads(db))
    it = db.get_item(mid)
    assert it.state is State.DONE
    assert os.readlink(it.file_path).endswith(H("b2") + "/The.Matrix.1999.1080p.WEB-DL.mkv")
    assert fe.deleted == []


def test_replace_keeps_old_until_new_ready(env, db):
    fe, results, tmp = env
    results += [T("The.Matrix.1999.1080p.WEB-DL", "old", 4)]
    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    run(acquire.search_wanted(db))
    fe.ready(H("old"), ["m.mkv"])
    run(acquire.check_downloads(db))
    old_link = db.get_item(mid).file_path

    assert plex_issues.start_replace(db, mid, "buffering")
    results.append(T("The.Matrix.1999.1080p.BluRay", "new", 8))
    run(acquire.search_wanted(db))
    it = db.get_item(mid)
    assert it.state is State.DOWNLOADING and it.engine_id == H("new") and it.prev_engine_id == H("old")
    assert os.path.islink(old_link)  # still playable meanwhile

    fe.ready(H("new"), ["n.mkv"])
    run(acquire.check_downloads(db))
    it = db.get_item(mid)
    assert it.state is State.DONE and it.mode is None and it.prev_engine_id is None
    assert os.readlink(it.file_path).endswith(H("new") + "/n.mkv")
    assert H("old") in fe.deleted


def test_failed_download_goes_back_and_skips_release(env, db):
    fe, results, tmp = env
    results += [T("The.Matrix.1999.1080p.WEB-DL", "bad", 4)]
    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    run(acquire.search_wanted(db))
    fe.entries[H("bad")].status = "error"
    run(acquire.check_downloads(db))
    assert db.get_item(mid).state is State.WANTED
    run(acquire.search_wanted(db))  # only result is the failed one -> nothing to try
    it = db.get_item(mid)
    assert it.state is State.WAITING and it.attempts == 1
    assert it.next_attempt_at > time.time() + 3000


def test_season_pack_maps_files(env, db):
    fe, results, tmp = env
    results += [T("Breaking.Bad.S01.1080p.BluRay", "pack", 14)]
    ids = [db.add_item(kind="episode", tmdb_id=1396, title="Breaking Bad", year=2008,
                       season=1, episode=e) for e in (1, 2, 3)]
    run(acquire.search_wanted(db))
    assert {db.get_item(i).engine_id for i in ids} == {H("pack")}
    fe.ready(H("pack"), ["Breaking.Bad.S01E01.mkv", "Breaking.Bad.S01E02.mkv"])
    run(acquire.check_downloads(db))
    states = [db.get_item(i).state for i in ids]
    assert states == [State.DONE, State.DONE, State.WANTED]
    assert H("pack") not in fe.deleted  # still used by the two done episodes


def test_upgrade_needs_clear_margin(env, db):
    fe, results, tmp = env
    results += [T("The.Matrix.1999.720p.WEB", "low", 2)]
    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    run(acquire.search_wanted(db))
    fe.ready(H("low"), ["m.mkv"])
    run(acquire.check_downloads(db))
    from app.repair import upgrades
    run(upgrades.sweep(db))
    assert db.get_item(mid).state is State.DONE  # nothing better
    results.append(T("The.Matrix.1999.1080p.BluRay", "hi", 10))
    run(upgrades.sweep(db))
    it = db.get_item(mid)
    assert it.state is State.DOWNLOADING and it.mode == "upgrade" and it.prev_engine_id == H("low")
    fe.entries[H("hi")].status = "error"
    run(acquire.check_downloads(db))
    it = db.get_item(mid)
    assert it.state is State.DONE and it.engine_id == H("low") and os.path.islink(it.file_path)


def test_engine_down_does_not_burn_releases(env, db):
    fe, results, tmp = env
    results += [T("The.Matrix.1999.1080p.WEB-DL", "x1", 4)]
    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    fe.down = True
    run(acquire.search_wanted(db))
    assert db.get_item(mid).state is State.WANTED
    assert db.query("SELECT COUNT(*) n FROM releases_tried")[0]["n"] == 0

    async def boom(magnet):
        raise engine.EngineDown("gone mid-search")
    fe.down = False
    orig = engine.add_magnet
    engine.add_magnet = boom
    try:
        run(acquire.search_wanted(db))
    finally:
        engine.add_magnet = orig
    assert db.get_item(mid).state is State.WANTED
    assert db.query("SELECT COUNT(*) n FROM releases_tried")[0]["n"] == 0
