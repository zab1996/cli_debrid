from app import search
from app.models import State


def _ep(db, s, e, tmdb=1396):
    return db.add_item(kind="episode", tmdb_id=tmdb, title="Breaking Bad", season=s, episode=e,
                       imdb_id="tt0903747")


def test_plan_groups_seasons(db, monkeypatch):
    from app import config
    cfg = config.Config()
    cfg.debrid.provider, cfg.debrid.api_key = "realdebrid", "k"
    monkeypatch.setattr(config, "_current", cfg)
    m = db.add_item(kind="movie", tmdb_id=603, title="The Matrix", year=1999)
    a, b, c = _ep(db, 1, 1), _ep(db, 1, 2), _ep(db, 2, 1)
    _ep(db, 1, 3)  # exists but not passed in -> still counts toward pack size
    items = [db.get_item(i) for i in (m, a, b, c)]
    jobs = search.plan(db, items)
    kinds = sorted((j.query.kind, len(j.items)) for j in jobs)
    assert kinds == [("episode", 1), ("movie", 1), ("season", 2)]
    season = next(j for j in jobs if j.query.kind == "season")
    assert season.query.episode_count == 3 and season.query.episode is None
    assert [j.query.kind for j in search.split(db, season)] == ["episode", "episode"]


def test_tried_keys_excludes_failed_only(db):
    i = db.add_item(kind="movie", tmdb_id=603, title="The Matrix")
    with db.tx() as c:
        c.execute("INSERT INTO releases_tried VALUES (?,?,?,?,?,?)", (i, "aaa", "x", 1, "failed", 0))
        c.execute("INSERT INTO releases_tried VALUES (?,?,?,?,?,?)", (i, "bbb", "y", 1, "picked", 0))
    assert search.tried_keys(db, [db.get_item(i)]) == {"aaa"}
    assert db.get_item(i).state is State.WANTED


def test_season_sweep_job_even_without_debrid(db):
    items = [db.get_item(_ep(db, 1, e)) for e in (1, 2, 3)]
    jobs = search.plan(db, items)
    assert [(j.query.kind, len(j.items)) for j in jobs] == [("season", 3)]
