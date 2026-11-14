import asyncio
import json
import logging

from app import config, engine_config, engine_proc


def test_supervisor_restarts_and_forwards_logs(tmp_path, monkeypatch, caplog):
    script = tmp_path / "fake-engine"
    script.write_text("#!/bin/sh\nprintf '\\033[32mINFO\\033[0m hello from engine\\n'\nexit 3\n")
    script.chmod(0o755)
    cfg = config.Config()
    cfg.debrid.provider, cfg.debrid.api_key = "realdebrid", "k"
    cfg.mount.path = str(tmp_path / "mnt")
    monkeypatch.setattr(config, "_current", cfg)
    monkeypatch.setattr(engine_proc, "BACKOFF", [0.05])

    async def go():
        p = engine_proc.EngineProcess(str(script))
        p.start()
        await asyncio.sleep(0.6)
        await p.stop()
        return p
    with caplog.at_level(logging.INFO, logger="clid2.engine.out"):
        p = asyncio.run(go())
    assert p.restarts >= 2 and p.last_exit == 3
    assert any(r.getMessage() == "INFO hello from engine" for r in caplog.records)
    written = json.loads((config.DATA_DIR / "engine" / "config.json").read_text())
    assert written["debrids"][0]["api_key"] == "k" and written["mount"]["mount_path"] == cfg.mount.path
    assert oct((config.DATA_DIR / "engine" / "config.json").stat().st_mode & 0o777) == "0o600"


def test_not_ready_without_providers():
    assert not engine_config.ready(config.Config())
    c = config.Config(usenet=[config.UsenetProvider(host="h", connections=8)],
                      advanced={"engine": {"log_level": "debug", "mount": {"dfs": {"chunk_size": "8MB"}}}})
    assert engine_config.ready(c)
    built = engine_config.build(c, config.DATA_DIR / "engine")
    assert built["usenet"]["max_connections"] == 8 and built["log_level"] == "debug"
    assert built["mount"]["dfs"]["chunk_size"] == "8MB" and built["mount"]["type"] == "dfs"


def test_missing_binary_is_dev_mode(tmp_path):
    async def go():
        p = engine_proc.EngineProcess(str(tmp_path / "nope"))
        p.start()
        assert not p.available and not p.running
        await p.stop()
    asyncio.run(go())
