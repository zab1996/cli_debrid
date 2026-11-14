import asyncio
import time

from app.models import State
from app.pipeline import register
from app.worker import Worker


def test_steps_respect_cadence_and_survive_errors():
    w = Worker(tick=0.01)
    calls = []

    @w.every(60)
    async def slow():
        calls.append("slow")

    @w.every(0)
    async def boom():
        calls.append("boom")
        raise RuntimeError("bad")

    asyncio.run(w.run_once(now=1000))
    asyncio.run(w.run_once(now=1010))
    asyncio.run(w.run_once(now=1061))
    assert calls.count("slow") == 2
    assert calls.count("boom") == 3
    assert w.steps[1].last_error == "RuntimeError: bad"


def test_run_forever_stops():
    async def go():
        w = Worker(tick=0.01)
        task = asyncio.create_task(w.run_forever())
        await asyncio.sleep(0.05)
        w.stop()
        await asyncio.wait_for(task, 1)
    asyncio.run(go())


def test_wake_waiting_and_failed_retry(db):
    w = Worker()
    register(w, db)
    wait_id = db.add_item(kind="movie", tmdb_id=1, title="Wait")
    fail_id = db.add_item(kind="movie", tmdb_id=2, title="Fail")
    db.set_state(wait_id, State.WAITING, next_attempt_at=time.time() - 5)
    db.set_state(fail_id, State.FAILED)

    asyncio.run(w.run_once())
    assert db.get_item(wait_id).state is State.WANTED
    failed = db.get_item(fail_id)
    assert failed.state is State.FAILED and failed.next_attempt_at > time.time() + 6 * 86400

    db.set_state(fail_id, State.FAILED, next_attempt_at=time.time() - 1, attempts=10)
    for s in w.steps:
        s.last_run = 0
    asyncio.run(w.run_once())
    again = db.get_item(fail_id)
    assert again.state is State.WANTED and again.attempts == 0
