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
            "dfs": dfs_settings(cfg, engine_dir),
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
        m = cfg.mount
        out["usenet"].update({
            "max_connections": m.usenet_max_connections,               # per streaming file
            "processing_max_connections": m.usenet_processing_connections,
            "read_ahead": m.usenet_read_ahead,
            "buffer_memory": m.usenet_buffer_memory,
            "interactive_pool_reserve_enabled": m.usenet_interactive_reserve,
            "interactive_pool_reserve_percent": m.usenet_interactive_reserve_percent,
            "processing_timeout": m.usenet_processing_timeout,
            "availability_sample_percent": m.usenet_availability_sample_percent,
            "deobfuscate_mode": m.usenet_deobfuscate_mode,
        })
    _merge(out, cfg.adv("engine", {}) or {})
    return out


def dfs_settings(cfg: Config, engine_dir: Path) -> dict:
    m = cfg.mount
    d = {
        "cache_dir": str(engine_dir / "cache"),
        "disk_cache_size": m.disk_cache_size,
        "cache_expiry": m.cache_expiry,
        "cache_cleanup_interval": m.cache_cleanup_interval,
        "chunk_size": m.chunk_size,
        "read_ahead_size": m.read_ahead_size,
        "uid": m.uid, "gid": m.gid, "umask": m.umask,
    }
    if m.buffer_memory:
        d["buffer_memory"] = m.buffer_memory
    if m.prewarm_next_episode and cfg.plex.url and cfg.plex.token:
        d.update(prewarm_next_episode=True, plex_url=cfg.plex.url, plex_token=cfg.plex.token)
    return d


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def write(cfg: Config, engine_dir: Path) -> bool:
    """Write the engine's config.json. Returns True only if its content changed
    (callers restart the mount only then)."""
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "downloads").mkdir(exist_ok=True)
    (engine_dir / "cache").mkdir(exist_ok=True)
    path = engine_dir / "config.json"
    text = json.dumps(build(cfg, engine_dir), indent=2)
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)  # holds API keys and usenet passwords
    os.replace(tmp, path)
    return True


def remove(engine_dir: Path) -> bool:
    """No providers left: drop the config so the engine isn't started again."""
    try:
        (engine_dir / "config.json").unlink()
        return True
    except FileNotFoundError:
        return False
