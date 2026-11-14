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
    q = client.get("/queues")
    assert q.status_code == 200 and "Breaking Bad" in q.text and "Failed" in q.text
    page = client.get("/settings")
    assert page.status_code == 200 and "DFS streaming" in page.text and "Fallback version" in page.text

    for page in ("/database", "/database?kind=episode&state=Failed&q=Breaking", "/blacklist", "/calendar"):
        assert client.get(page).status_code == 200, page
    assert "Breaking Bad" in client.get("/database?state=Failed").text
    r = client.post("/database/bulk", data={"action": "wanted", "ids": [str(eid)],
                                            "back": "/database?state=Failed"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert db.get_item(eid).state is State.WANTED
    db.set_state(eid, State.FAILED)
    client.post(f"/item/{eid}/retry")
    assert db.get_item(eid).state is State.WANTED
    client.post("/show/1396/delete", data={"block": "1"})
    assert db.get_item(eid) is None
    assert "Breaking Bad" in client.get("/blacklist").text


def test_settings_roundtrip(client):
    _configure()
    form = {
        "tmdb_api_key": "newkey", "tvdb_api_key": "", "debrid.provider": "torbox", "debrid.api_key": "tb",
        "usenet-present": "1", "usenet-0-host": "news.example.com", "usenet-0-port": "563",
        "usenet-0-ssl": "on", "usenet-0-username": "u", "usenet-0-password": "p",
        "usenet-0-connections": "20", "usenet-1-host": "",
        "mount-present": "1", "mount.path": "/mnt/x", "mount.disk_cache_size": "20GB",
        "mount.chunk_size": "16MB", "mount.usenet_max_connections": "12",
        "indexers-present": "1", "indexers-0-type": "torrentio", "indexers-0-enabled": "on",
        "indexers-1-type": "newznab", "indexers-1-url": "https://idx", "indexers-1-api_key": "k",
        "indexers-2-type": "", "library.mode": "symlink", "library.movies_path": "/m",
        "library.shows_path": "/s", "plex.url": "", "plex.token": "",
        "version-present": "1", "quality.name": "4k", "quality.max_resolution": "2160p",
        "quality.resolution_wanted": "<=", "quality.enable_hdr": "on", "quality.language": "en",
        "quality.min_size_gb": "0", "quality.max_size_gb": "8", "quality.min_bitrate_mbps": "2",
        "quality.max_bitrate_mbps": "",
        "quality.filter_out": "HDCAM\n(?-i:WEB-DL)  [debrid]\n\n", "quality.filter_in": "",
        "quality.preferred_filter_in": "50 | (?i)remux", "quality.preferred_filter_out": "500 | (?i)audio.?description",
        "quality.bitrate_weight": "5", "fallback.enabled": "on", "fallback.name": "1080p fb",
        "fallback.max_resolution": "1080p", "fallback.resolution_wanted": "==", "fallback.min_size_gb": "1",
        "indexers-0-priority": "30",
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
    q = c.quality
    assert (q.name, q.max_resolution, q.resolution_wanted, q.enable_hdr, q.max_size_gb) == ("4k", "2160p", "<=", True, 8)
    assert [(f.pattern, f.source) for f in q.filter_out] == [("HDCAM", "both"), ("(?-i:WEB-DL)", "debrid")]
    assert [(f.weight, f.pattern) for f in q.preferred_filter_in] == [(50, "(?i)remux")]
    assert q.bitrate_weight == 5 and q.min_bitrate_mbps == 2
    assert q.fallback.name == "1080p fb" and q.fallback.max_resolution == "1080p" and q.fallback.min_size_gb == 1
    assert q.fallback.filter_out == q.filter_out
    assert c.indexers[0].priority == 30
    assert (c.mount.path, c.mount.disk_cache_size, c.mount.chunk_size, c.mount.usenet_max_connections) == \
        ("/mnt/x", "20GB", "16MB", 12)
    assert c.mount.prewarm_next_episode is False  # checkbox not sent = off
    assert c.mount.read_ahead_size == "32MB"      # untouched fields keep their values
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
