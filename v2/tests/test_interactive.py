import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import config, interactive, scrapers
from app.downloads import engine
from app.metadata import tmdb
from app.models import State
from app.ranking import Release


def H(s):
    return hashlib.sha1(s.encode()).hexdigest()


@pytest.fixture
def env(monkeypatch, fake_tmdb):
    cfg = config.Config(tmdb_api_key="k", indexers=[config.IndexerConfig(type="torrentio")])
    cfg.debrid.provider, cfg.debrid.api_key = "realdebrid", "x"
    cfg.usenet = [config.UsenetProvider(host="h")]
    monkeypatch.setattr(config, "_current", cfg)
    results = [
        Release(title="The.Matrix.1999.1080p.BluRay.x264", protocol="torrent", indexer="t", size_gb=9, info_hash=H("a")),
        Release(title="The.Matrix.1999.HDCAM", protocol="torrent", indexer="t", size_gb=1, info_hash=H("b")),
        Release(title="The.Matrix.1999.2160p.WEB", protocol="nzb", indexer="n", size_gb=20, nzb_url="u", guid="g"),
    ]

    async def search_all(q):
        return [Release(**r.__dict__) for r in results]
    monkeypatch.setattr(scrapers, "search_all", search_all)
    added = []

    async def add_magnet(m):
        added.append(m)
        return scrapers.hash_from_magnet(m)

    async def cached(hs):
        return {h: h == H("a") for h in hs}
    monkeypatch.setattr(engine, "add_magnet", add_magnet)
    monkeypatch.setattr(engine, "cached", cached)

    async def details(db, kind, i):
        return {"kind": kind, "tmdb_id": i, "title": "The Matrix", "year": 1999, "overview": "x",
                "poster": None, "backdrop": None, "tagline": "", "genres": [], "runtime": 136,
                "status": "Released", "cast": [], "seasons": [], "networks": [], "rating": 8, "date": ""}

    async def browse(db, name, page=1):
        return [{"kind": "movie", "tmdb_id": 603, "title": "The Matrix", "year": 1999, "overview": "",
                 "poster": None, "rating": 8, "date": ""}]
    monkeypatch.setattr(tmdb, "details", details)
    monkeypatch.setattr(tmdb, "browse", browse)
    return added


def test_manual_search_lists_rejected_and_grab_adds_item(db, env):
    s = asyncio.run(interactive.run(db, "movie", 603))
    j = interactive.to_json(db, s)
    titles = [r["title"] for r in j["results"]]
    assert titles[0] == "The.Matrix.1999.1080p.BluRay.x264" and j["results"][0]["cached"]
    cam = next(r for r in j["results"] if "HDCAM" in r["title"])
    assert cam["reject"] == "cam/screener" and cam["score"] is None
    ok, msg, item_id = asyncio.run(interactive.grab(db, s.id, 0))
    assert ok and item_id
    it = db.get_item(item_id)
    assert it.state is State.DOWNLOADING and it.engine_id == H("a") and len(env) == 1
    # Grabbing again while it downloads is refused.
    ok, msg, _ = asyncio.run(interactive.grab(db, s.id, 0))
    assert not ok and "Already downloading" in msg


def test_grab_for_done_item_replaces(db, env):
    i = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    db.record_release(i, "old", "Old.Release", 1, "picked")
    db.set_state(i, State.DONE, engine_id="old", file_path="/x")
    s = asyncio.run(interactive.run(db, "movie", 603))
    ok, _, _ = asyncio.run(interactive.grab(db, s.id, 0))
    it = db.get_item(i)
    assert ok and it.mode == "replace" and it.prev_engine_id == "old"
    assert db.query("SELECT outcome FROM releases_tried WHERE release_key='old'")[0]["outcome"] == "replaced"


def test_pages(tmp_path, env):
    from app.main import create_app
    config.save(config.get())
    app = create_app(db_path=str(tmp_path / "w.db"), start_worker=False, start_engine=False)
    with TestClient(app) as c:
        assert "Trending Movies" in c.get("/discover").text
        assert "The Matrix" in c.get("/discover/movie/603").text
        assert c.get("/add", follow_redirects=False).headers["location"] == "/discover"
        assert c.get("/releases?kind=movie&tmdb_id=603").status_code == 200
        j = c.get("/api/releases?kind=movie&tmdb_id=603").json()
        assert len(j["results"]) == 3
        r = c.post("/api/releases/grab", json={"id": j["id"], "i": 0}).json()
        assert r["ok"]
        lib = c.get("/library").text
        assert "Media Library" in lib and "The Matrix" in lib
        assert c.get("/database").status_code == 200
        assert c.get("/api/releases?kind=episode&tmdb_id=1").status_code == 400
