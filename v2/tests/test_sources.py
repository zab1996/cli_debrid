import asyncio

import pytest

from app import catalog, sources
from app.config import Config, SourceConfig
from app.sources import mdblist, plex_watchlist, trakt


def test_trakt_paths():
    assert trakt.list_path(SourceConfig(type="trakt_list", value="https://trakt.tv/users/al/lists/sci-fi?x=1")) \
        == "/users/al/lists/sci-fi/items"
    assert trakt.list_path(SourceConfig(type="trakt_list", value="al/sci-fi")) == "/users/al/lists/sci-fi/items"
    assert trakt.list_path(SourceConfig(type="trakt_watchlist", value="al")) == "/users/al/watchlist"
    with pytest.raises(ValueError):
        trakt.list_path(SourceConfig(type="trakt_list", value="al"))


def test_mdblist_urls_and_rows():
    assert mdblist._public_url("al/top") == "https://mdblist.com/lists/al/top/json"
    assert mdblist._api_url("https://mdblist.com/lists/al/top") == "https://api.mdblist.com/lists/al/top/items"
    rows = mdblist._rows({"movies": [{"title": "A"}], "shows": [{"title": "B"}], "episodes": [{}]})
    assert [(r["title"], r["mediatype"]) for r in rows] == [("A", "movie"), ("B", "show")]


def test_plex_guids():
    assert plex_watchlist.ids_from_guids([{"id": "imdb://tt1"}, {"id": "tmdb://5"}, {"id": "x://y"}]) \
        == {"imdb": "tt1", "tmdb": "5"}


def test_sync_acts_once_per_request(db, monkeypatch):
    from app import config
    cfg = Config(sources=[SourceConfig(type="mdblist", value="al/top")])
    monkeypatch.setattr(config, "_current", cfg)
    calls = []

    async def fetch(db_, s):
        return [sources.Request("movie", 603, "The Matrix"), sources.Request("show", 1396, "BB")]

    async def add_movie(db_, i, source):
        calls.append(("movie", i, source))
        return 1

    async def add_show(db_, i, source):
        calls.append(("show", i, source))
        return 3
    monkeypatch.setattr(sources, "fetch", fetch)
    monkeypatch.setattr(catalog, "add_movie", add_movie)
    monkeypatch.setattr(catalog, "add_show", add_show)
    asyncio.run(sources.sync_all(db))
    asyncio.run(sources.sync_all(db))
    assert calls == [("movie", 603, "mdblist:al/top"), ("show", 1396, "mdblist:al/top")]
