import asyncio
import time
from datetime import date, timedelta

import pytest

from app import catalog
from app.metadata import tmdb, tvdb
from app.models import State


def run(coro):
    return asyncio.run(coro)


def _day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


@pytest.fixture
def fake_tmdb(monkeypatch):
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


def test_home_release_date_prefers_digital():
    rd = {"results": [{"release_dates": [{"type": 3, "release_date": "2020-01-10T00:00:00"},
                                         {"type": 4, "release_date": "2020-03-01T00:00:00"}]},
                      {"release_dates": [{"type": 5, "release_date": "2020-02-15T00:00:00"}]}]}
    assert tmdb.home_release_date(rd, None) == "2020-02-15"
    only_cinema = {"results": [{"release_dates": [{"type": 3, "release_date": "2020-01-01"}]}]}
    assert tmdb.home_release_date(only_cinema, None) == "2020-03-16"
    assert tmdb.home_release_date({}, None) is None


def test_add_movie_released_and_future(db, fake_tmdb):
    a = run(catalog.add_movie(db, 603))
    assert db.get_item(a).state is State.WANTED
    b = run(catalog.add_movie(db, 999))
    item = db.get_item(b)
    assert item.state is State.WAITING and item.next_attempt_at > time.time()
    assert run(catalog.add_movie(db, 603)) is None


def test_add_show_states(db, fake_tmdb):
    assert run(catalog.add_show(db, 1396)) == 3
    eps = {(i.season, i.episode): i for s in State for i in db.items_in(s)}
    assert eps[(1, 1)].state is State.WANTED
    assert eps[(1, 2)].state is State.WAITING
    assert eps[(2, 1)].state is State.WAITING
    assert eps[(1, 1)].label == "Breaking Bad S01E01"


def test_partial_seasons_then_widen(db, fake_tmdb):
    assert run(catalog.add_show(db, 1396, seasons=[2])) == 1
    assert run(catalog.add_show(db, 1396, seasons=[1])) == 2
    assert run(catalog.add_show(db, 1396)) == 0


def test_sync_updates_air_date_of_waiting(db, fake_tmdb):
    run(catalog.add_show(db, 1396))
    fake_tmdb[1396]["seasons"]["2"][0]["air_date"] = _day(-3)
    fake_tmdb[1396]["seasons"]["2"].append({"episode": 2, "air_date": _day(-2), "title": "x"})
    assert run(catalog.sync_show(db, 1396)) == 1
    rows = db.query("SELECT state, release_date FROM items WHERE season=2 AND episode=1")
    assert rows[0]["state"] == "Wanted" and rows[0]["release_date"] == _day(-3)
