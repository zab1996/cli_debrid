import time

from app.models import State, next_backoff


def test_add_item_dedupes_movies_and_episodes(db):
    a = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    assert a is not None
    assert db.add_item(kind="movie", tmdb_id=603, title="The Matrix") is None
    e1 = db.add_item(kind="episode", tmdb_id=1396, title="Breaking Bad", season=1, episode=1)
    e2 = db.add_item(kind="episode", tmdb_id=1396, title="Breaking Bad", season=1, episode=2)
    assert e1 and e2 and e1 != e2
    assert db.add_item(kind="episode", tmdb_id=1396, title="Breaking Bad", season=1, episode=1) is None


def test_state_transitions_and_counts(db):
    i = db.add_item(kind="movie", tmdb_id=1, title="X")
    assert db.get_item(i).state is State.WANTED
    db.set_state(i, State.DOWNLOADING, engine_id="abc", release_name="X.2020.1080p")
    item = db.get_item(i)
    assert item.state is State.DOWNLOADING and item.engine_id == "abc"
    counts = db.state_counts()
    assert counts["Downloading"] == 1 and counts["Wanted"] == 0


def test_set_state_rejects_unknown_fields(db):
    import pytest
    i = db.add_item(kind="movie", tmdb_id=1, title="X")
    with pytest.raises(ValueError):
        db.set_state(i, State.DONE, title="nope")


def test_due_only_filters_future(db):
    a = db.add_item(kind="movie", tmdb_id=1, title="A")
    b = db.add_item(kind="movie", tmdb_id=2, title="B")
    db.set_state(a, State.WAITING, next_attempt_at=time.time() - 1)
    db.set_state(b, State.WAITING, next_attempt_at=time.time() + 3600)
    assert [x.id for x in db.items_in(State.WAITING, due_only=True)] == [a]


def test_events_are_capped(db):
    for n in range(1100):
        db.log_event(f"e{n}")
    assert len(db.query("SELECT id FROM events")) <= 1001
    assert db.recent_events(1)[0]["message"] == "e1099"


def test_backoff():
    assert next_backoff(0) == 0
    assert next_backoff(1) == 3600
    assert next_backoff(3) == 24 * 3600
    assert next_backoff(9) == 24 * 3600
