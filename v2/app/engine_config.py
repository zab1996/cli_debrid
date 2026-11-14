"""Builds the engine's config.json from our settings.

The engine is never configured by hand: this file is regenerated before
every engine start. Anything not set here uses the engine's own defaults.
Hidden engine tweaks can be passed through `advanced: {engine: {...}}`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Config

ENGINE_PORT = os.environ.get("CLID2_ENGINE_PORT", "8283")


def ready(cfg: Config) -> bool:
    """The engine needs at least one debrid key or usenet provider."""
    return bool((cfg.debrid.provider and cfg.debrid.api_key) or cfg.usenet)


def build(cfg: Config, engine_dir: Path) -> dict:
    out: dict = {
        "bind_address": "127.0.0.1",
        "port": ENGINE_PORT,
        "log_level": cfg.adv("engine_log_level", "info"),
        "use_auth": False,
        "download_folder": str(engine_dir / "downloads"),
        "default_download_action": "none",
        "skip_multi_season": True,
        "debrids": [],
        "usenet": {"providers": []},
        "mount": {
            "type": "dfs",
            "mount_path": cfg.mount.path,
            "dfs": {
                "cache_dir": str(engine_dir / "cache"),
                "disk_cache_size": f"{cfg.mount.cache_size_gb}GB",
            },
        },
    }
    if cfg.debrid.provider and cfg.debrid.api_key:
        out["debrids"].append({
            "name": cfg.debrid.provider,
            "provider": cfg.debrid.provider,
            "api_key": cfg.debrid.api_key,
        })
    for i, p in enumerate(cfg.usenet):
        out["usenet"]["providers"].append({
            "host": p.host, "port": p.port, "ssl": p.ssl,
            "username": p.username, "password": p.password,
            "max_connections": p.connections, "priority": i,
        })
    if cfg.usenet:
        out["usenet"]["max_connections"] = sum(p.connections for p in cfg.usenet)
    _merge(out, cfg.adv("engine", {}) or {})
    return out


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def write(cfg: Config, engine_dir: Path) -> Path:
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "downloads").mkdir(exist_ok=True)
    (engine_dir / "cache").mkdir(exist_ok=True)
    path = engine_dir / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(build(cfg, engine_dir), indent=2))
    os.chmod(tmp, 0o600)  # holds API keys and usenet passwords
    os.replace(tmp, path)
    return path
