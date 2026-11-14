import asyncio

import pytest

from app import blacklist, config, http, notify


class Resp:
    def __init__(self, code=204):
        self.status_code = code
        self.text = ""


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def request(method, url, **kw):
        calls.append((method, url, kw))
        return Resp(500 if "broken" in url else 204)
    monkeypatch.setattr(http, "request", request)
    return calls


def _cfg(monkeypatch, *targets, **on):
    cfg = config.Config(notifications=list(targets))
    for k, v in on.items():
        setattr(cfg.notify_on, k, v)
    monkeypatch.setattr(config, "_current", cfg)
    return cfg


def test_nothing_queued_without_targets_or_when_event_off(db, monkeypatch):
    _cfg(monkeypatch)
    notify.emit(db, "collected", "X")
    _cfg(monkeypatch, config.NotifyTarget(type="discord", url="https://d"), failed=False)
    notify.emit(db, "failed", "Y")
    assert db.query("SELECT COUNT(*) n FROM notifications")[0]["n"] == 0


def test_batched_per_event_and_formats(db, monkeypatch, sent):
    _cfg(monkeypatch, config.NotifyTarget(type="discord", url="https://discord/x"),
         config.NotifyTarget(type="telegram", token="T", chat_id="42"),
         config.NotifyTarget(type="ntfy", url="https://ntfy.sh/topic"))
    for e in (1, 2, 3):
        notify.emit(db, "collected", f"Breaking Bad S01E0{e}", "Breaking.Bad.S01.1080p")
    blacklist.block(db, "movie", 1, "Bad Movie", reason="deleted from library")
    asyncio.run(notify.flush(db))
    assert len(sent) == 6  # 2 events x 3 targets
    discord = [c for c in sent if c[1] == "https://discord/x"]
    embed = discord[0][2]["json"]["embeds"][0]
    assert embed["title"] == "✅ Collected (3)" and "Breaking Bad S01E02" in embed["description"]
    tg = [c for c in sent if "telegram" in c[1]][0]
    assert tg[1].endswith("/botT/sendMessage") and tg[2]["json"]["chat_id"] == "42"
    ntfy = [c for c in sent if "ntfy" in c[1]][0]
    assert ntfy[2]["headers"]["Title"] in ("Collected (3)", "Blacklisted")
    assert db.query("SELECT COUNT(*) n FROM notifications")[0]["n"] == 0


def test_failed_sends_retry_then_drop(db, monkeypatch, sent):
    _cfg(monkeypatch, config.NotifyTarget(type="webhook", url="https://broken"))
    notify.emit(db, "collected", "X")
    for _ in range(notify.MAX_TRIES - 1):
        asyncio.run(notify.flush(db))
        assert db.query("SELECT COUNT(*) n FROM notifications")[0]["n"] == 1
    asyncio.run(notify.flush(db))
    assert db.query("SELECT COUNT(*) n FROM notifications")[0]["n"] == 0


def test_long_batches_are_trimmed():
    rows = [{"title": f"T{i}", "detail": ""} for i in range(40)]
    heading, lines = notify._message("collected", rows)
    assert heading == "✅ Collected (40)" and len(lines) == notify.MAX_LINES + 1
    assert lines[-1] == "…and 15 more"
