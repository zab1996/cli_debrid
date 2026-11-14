import base64

import pytest
from fastapi.testclient import TestClient

from app import config
from app.models import State


@pytest.fixture
def client(tmp_path):
    from app.main import create_app
    app = create_app(db_path=str(tmp_path / "web.db"), start_worker=False, start_engine=False)
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def _configure():
    cfg = config.Config(tmdb_api_key="k", indexers=[config.IndexerConfig(type="torrentio")])
    cfg.debrid.provider, cfg.debrid.api_key = "realdebrid", "x"
    config.save(cfg)


def test_unconfigured_home_goes_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    for step in ("metadata", "downloads", "indexers", "library"):
        assert client.get(f"/setup?step={step}").status_code == 200


def test_pages_render(client):
    _configure()
    db = client.app_ref.state.db
    names = {s["name"] for s in client.get("/health").json()["steps"]}
    assert {"search_wanted", "wake_waiting", "sync_sources", "plex_reported_issues"} <= names

    mid = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    db.log_event("Added The Matrix", mid)
    with db.tx() as c:
        c.execute("INSERT INTO shows(tmdb_id, title, added_at) VALUES (1396, 'Breaking Bad', 0)")
    eid = db.add_item(kind="episode", tmdb_id=1396, title="Breaking Bad", season=1, episode=1)
    db.set_state(eid, State.FAILED)

    home = client.get("/")
    assert home.status_code == 200 and "Added The Matrix" in home.text
    lib = client.get("/library")
    assert "The Matrix" in lib.text and "Breaking Bad" in lib.text
    assert "The Matrix" not in client.get("/library?state=Failed").text
    assert "S01" in client.get(f"/item/{eid}").text or "Breaking Bad" in client.get(f"/item/{eid}").text
    assert "Season 1" in client.get("/show/1396").text
    assert client.get("/add").status_code == 200
    assert client.get("/logs").status_code == 200
    assert client.get("/settings").status_code == 200

    client.post(f"/item/{eid}/retry")
    assert db.get_item(eid).state is State.WANTED
    client.post("/show/1396/delete")
    assert db.get_item(eid) is None


def test_settings_roundtrip(client):
    _configure()
    form = {
        "tmdb_api_key": "newkey", "tvdb_api_key": "", "debrid.provider": "torbox", "debrid.api_key": "tb",
        "usenet-present": "1", "usenet-0-host": "news.example.com", "usenet-0-port": "563",
        "usenet-0-ssl": "on", "usenet-0-username": "u", "usenet-0-password": "p",
        "usenet-0-connections": "20", "usenet-1-host": "",
        "mount.path": "/mnt/x", "mount.cache_size_gb": "20",
        "indexers-present": "1", "indexers-0-type": "torrentio", "indexers-0-enabled": "on",
        "indexers-1-type": "newznab", "indexers-1-url": "https://idx", "indexers-1-api_key": "k",
        "indexers-2-type": "", "library.mode": "symlink", "library.movies_path": "/m",
        "library.shows_path": "/s", "plex.url": "", "plex.token": "", "quality.preset": "4k_1080p",
        "quality.language": "en", "quality.min_size_gb": "0", "quality.max_size_gb": "8",
        "quality.never_contain": "HDCAM, 3D", "quality.must_contain": "",
        "sources-present": "1", "sources-0-type": "plex_watchlist", "sources-0-enabled": "on",
        "sources-1-type": "mdblist", "sources-1-value": "", "trakt_client_id": "", "mdblist_api_key": "",
        "upgrade_days": "3", "web_password": "",
    }
    r = client.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text[r.text.find("notice"):][:400]
    c = config.load()
    assert c.tmdb_api_key == "newkey" and c.debrid.provider == "torbox"
    assert [(p.host, p.connections, p.ssl) for p in c.usenet] == [("news.example.com", 20, True)]
    assert [(i.type, i.enabled) for i in c.indexers] == [("torrentio", True), ("newznab", False)]
    assert c.quality.never_contain == ["HDCAM", "3D"] and c.quality.max_size_gb == 8
    assert [s.type for s in c.sources] == ["plex_watchlist"]
    assert c.upgrade_days == 3


def test_password(client):
    _configure()
    c = config.get()
    c.web_password = "s3cret"
    config.save(c)
    assert client.get("/").status_code == 401
    assert client.get("/health").status_code == 200
    good = base64.b64encode(b"any:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
    bad = base64.b64encode(b"any:nope").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
