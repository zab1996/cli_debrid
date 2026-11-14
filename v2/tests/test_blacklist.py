import asyncio

import pytest

from app import blacklist, catalog, manage, search
from app.models import State


def run(c):
    return asyncio.run(c)


def test_blocked_movie_never_added(db, fake_tmdb):
    blacklist.block(db, "movie", 603, "The Matrix")
    with pytest.raises(catalog.Blacklisted):
        run(catalog.add_movie(db, 603))
    assert run(catalog.add_movie(db, 603, source="mdblist:x")) is None
    blacklist.unblock(db, "movie", 603)
    assert run(catalog.add_movie(db, 603)) is not None


def test_blocked_season_and_deleted_episodes_stay_gone(db, fake_tmdb):
    run(catalog.add_show(db, 1396))
    run(manage.delete_season(db, 1396, 2, block=True))
    one = db.query("SELECT id FROM items WHERE season=1 AND episode=1")[0]["id"]
    run(manage.delete_items(db, [one]))
    run(catalog.sync_show(db, 1396))  # daily refresh must not bring them back
    left = {(r["season"], r["episode"]) for r in db.query("SELECT season, episode FROM items")}
    assert left == {(1, 2)}
    assert blacklist.is_blocked(db, "show", 1396, 2) and not blacklist.is_blocked(db, "show", 1396)
    run(catalog.add_show(db, 1396, seasons=[1]))  # re-adding by hand restores deleted episodes
    left = {(r["season"], r["episode"]) for r in db.query("SELECT season, episode FROM items")}
    assert left == {(1, 1), (1, 2)}


def test_delete_and_block_movie(db):
    i = db.add_item(kind="movie", tmdb_id=603, title="The Matrix")
    run(manage.bulk(db, "delete_block", [i]))
    assert db.get_item(i) is None and blacklist.is_blocked(db, "movie", 603)


def test_release_blacklist_is_global_and_replaces_users(db):
    a = db.add_item(kind="movie", tmdb_id=1, title="A")
    b = db.add_item(kind="movie", tmdb_id=2, title="B")
    db.record_release(a, "hash1", "A.2020.1080p", 50, "picked")
    db.set_state(a, State.DONE, file_path="/x")
    run(manage.block_release(db, a, "hash1"))
    assert "hash1" in search.tried_keys(db, [db.get_item(b)])
    it = db.get_item(a)
    assert it.state is State.WANTED and it.mode == "replace"
