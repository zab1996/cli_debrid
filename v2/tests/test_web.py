from fastapi.testclient import TestClient


def test_home_and_health(tmp_path):
    from app.main import create_app
    app = create_app(db_path=str(tmp_path / "web.db"), start_worker=False, start_engine=False)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["ok"] is True
        names = {s["name"] for s in r.json()["steps"]}
        assert {"search_wanted", "wake_waiting", "sync_sources"} <= names

        app.state.db.add_item(kind="movie", tmdb_id=603, title="The Matrix")
        app.state.db.log_event("Added The Matrix")
        page = client.get("/")
        assert page.status_code == 200
        assert "Added The Matrix" in page.text
        assert "Wanted" in page.text
