"""All settings live in one YAML file.

Only the fields below are shown in the UI. Anything else in config.yaml under
`advanced:` is read by code that asks for it and is never shown.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("CLID2_DATA", "/data"))
CONFIG_PATH = Path(os.environ.get("CLID2_CONFIG", str(DATA_DIR / "config.yaml")))


class PlexConfig(BaseModel):
    url: str = ""
    token: str = ""


class DebridConfig(BaseModel):
    provider: Literal["", "realdebrid", "alldebrid", "torbox", "premiumize", "debridlink"] = ""
    api_key: str = ""


class UsenetProvider(BaseModel):
    host: str
    port: int = 563
    ssl: bool = True
    username: str = ""
    password: str = ""
    connections: int = 10


class MountConfig(BaseModel):
    path: str = "/mnt/clid2"
    cache_size_gb: int = 10


class LibraryConfig(BaseModel):
    mode: Literal["symlink", "plex"] = "symlink"
    movies_path: str = "/media/movies"
    shows_path: str = "/media/shows"


class SourceConfig(BaseModel):
    type: Literal["plex_watchlist", "trakt_list", "trakt_watchlist", "mdblist"]
    value: str = ""  # list URL / id; empty for "my watchlist" sources
    enabled: bool = True


class IndexerConfig(BaseModel):
    type: Literal["torrentio", "zilean", "jackett", "prowlarr", "newznab"]
    url: str = ""
    api_key: str = ""
    enabled: bool = True


class QualityConfig(BaseModel):
    preset: Literal["1080p", "4k", "4k_1080p"] = "1080p"
    min_size_gb: float = 0.0
    max_size_gb: float = 0.0  # 0 = no limit
    language: str = "en"
    must_contain: list[str] = Field(default_factory=list)
    never_contain: list[str] = Field(default_factory=list)


def configured(cfg: "Config") -> bool:
    """Enough set up to do anything: metadata + a way to download + an indexer."""
    return bool(cfg.tmdb_api_key and ((cfg.debrid.provider and cfg.debrid.api_key) or cfg.usenet)
                and cfg.indexers)


class Config(BaseModel):
    plex: PlexConfig = Field(default_factory=PlexConfig)
    debrid: DebridConfig = Field(default_factory=DebridConfig)
    usenet: list[UsenetProvider] = Field(default_factory=list)
    mount: MountConfig = Field(default_factory=MountConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    tmdb_api_key: str = ""
    tvdb_api_key: str = ""
    trakt_client_id: str = ""
    mdblist_api_key: str = ""
    sources: list[SourceConfig] = Field(default_factory=list)
    indexers: list[IndexerConfig] = Field(default_factory=list)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    upgrade_days: int = 7
    web_password: str = ""  # empty = no login (keep the app on a private network)
    advanced: dict[str, Any] = Field(default_factory=dict)

    def adv(self, key: str, default: Any) -> Any:
        """Read a hidden tweak from `advanced:` with a code default."""
        return self.advanced.get(key, default)


_lock = threading.Lock()
_current: Config | None = None


def load(path: Path | None = None) -> Config:
    global _current
    path = path or CONFIG_PATH
    with _lock:
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            _current = Config.model_validate(raw)
        else:
            _current = Config()
        return _current


def get() -> Config:
    return _current if _current is not None else load()


def save(cfg: Config, path: Path | None = None) -> None:
    global _current
    path = path or CONFIG_PATH
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
        os.replace(tmp, path)
        _current = cfg
