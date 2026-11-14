import os
import sys
from pathlib import Path

import pytest
from datetime import date, timedelta

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_current", None)
    os.environ.setdefault("CLID2_LOG_LEVEL", "WARNING")
    yield


@pytest.fixture
def db(tmp_path):
    from app.db import DB
    d = DB(tmp_path / "test.db")
    yield d
    d.close()


def _day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


@pytest.fixture
def fake_tmdb(monkeypatch):
    from app.metadata import tmdb, tvdb
    shows = {
        1396: {"tmdb_id": 1396, "title": "Breaking Bad", "original_title": "Breaking Bad",
               "original_language": "en", "year": 2008, "imdb_id": "tt0903747", "tvdb_id": 81189,
               "status": "Returning Series", "is_anime": False, "aliases": [],
               "seasons": {"1": [{"episode": 1, "air_date": _day(-400), "title": "Pilot"},
                                 {"episode": 2, "air_date": _day(5), "title": "Two"}],
                           "2": [{"episode": 1, "air_date": None, "title": "TBA"}]}},
    }
    movies = {
        603: {"tmdb_id": 603, "title": "The Matrix", "original_title": "The Matrix",
              "original_language": "en", "year": 1999, "imdb_id": "tt0133093", "runtime": 136,
              "release_date": "1999-09-21"},
        999: {"tmdb_id": 999, "title": "Future", "original_title": "Future",
              "original_language": "en", "year": 2030, "imdb_id": None, "runtime": 0,
              "release_date": _day(30)},
    }

    async def show(db, i):
        return shows[i]

    async def movie(db, i):
        return movies[i]
    monkeypatch.setattr(tmdb, "show", show)
    monkeypatch.setattr(tmdb, "movie", movie)
    monkeypatch.setattr(tvdb, "enabled", lambda: False)
    return shows


