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
from pydantic import BaseModel, Field, model_validator

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


_SIZE = r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB)$"
_DURATION = r"^(\d+(ms|s|m|h))+$"


class MountConfig(BaseModel):
    """The bundled mount engine (DFS). Defaults are the settings of the
    cli_mount instance this was ported from."""
    path: str = "/mnt/clid2"
    # DFS cache
    disk_cache_size: str = Field("100GB", pattern=_SIZE)
    cache_expiry: str = Field("2h", pattern=_DURATION)
    cache_cleanup_interval: str = Field("1m", pattern=_DURATION)
    # DFS streaming
    chunk_size: str = Field("8MB", pattern=_SIZE)
    read_ahead_size: str = Field("32MB", pattern=_SIZE)
    buffer_memory: str = Field("", pattern=r"^$|^0$|" + _SIZE[1:])   # "" = engine default (512MB)
    prewarm_next_episode: bool = True     # uses the Plex URL/token from the Plex settings
    # Files on the mount
    uid: int = 1000
    gid: int = 1000
    umask: str = Field("002", pattern=r"^[0-7]{3,4}$")
    # Usenet streaming
    usenet_max_connections: int = Field(20, ge=1, le=200)       # per streaming file
    usenet_processing_connections: int = Field(20, ge=1, le=200)
    usenet_read_ahead: str = Field("16MB", pattern=_SIZE)
    usenet_buffer_memory: str = Field("1GB", pattern=_SIZE)
    usenet_interactive_reserve: bool = True
    usenet_interactive_reserve_percent: int = Field(11, ge=0, le=100)
    usenet_processing_timeout: str = Field("5m", pattern=_DURATION)
    usenet_availability_sample_percent: int = Field(10, ge=1, le=100)
    usenet_deobfuscate_mode: Literal["season_ep", "full", "none"] = "season_ep"

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, d):
        if isinstance(d, dict) and "cache_size_gb" in d:
            d = dict(d)
            n = d.pop("cache_size_gb")
            d.setdefault("disk_cache_size", f"{n}GB")
        return d


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
    priority: float = 0   # added to every result's score (1.x "scraper priority")


class NotifyTarget(BaseModel):
    type: Literal["discord", "telegram", "ntfy", "pushover", "webhook"]
    url: str = ""        # discord/ntfy/webhook URL
    token: str = ""      # telegram bot token / pushover app token / ntfy access token
    chat_id: str = ""    # telegram chat id / pushover user key
    enabled: bool = True


class NotifyOn(BaseModel):
    collected: bool = True     # something new is in the library
    upgraded: bool = True      # a file was replaced (upgrade, Plex report, or by you)
    failed: bool = True        # gave up after repeated empty searches
    blacklisted: bool = True   # you blacklisted a title


class FilterPattern(BaseModel):
    """A v1-style filter: regex (or "quoted" plain text), scoped to a protocol."""
    pattern: str
    source: Literal["both", "nzb", "debrid"] = "both"


class WeightedPattern(BaseModel):
    pattern: str
    weight: float = 0


# The cli_debrid 1.x "1080p" version this was ported from (filters and all).
DEFAULT_FILTER_OUT = [
    "й", "я", "д", "г", "ё", "ж", "ф", "Ц", "Telesync", r"\.3D\.", "HD-TS", r"\.TS\.",
    r"\.CAM\.", "HDCAM", "Telecine", r"(?i)\bHDTS\b", "HDTC", "READNFO", "bilibili", "HINDI",
    "DoVi", "dem3nt3", r"\bAV1\b", r"(?i).*\bTS\b$",
]


def _default_filter_out() -> list[FilterPattern]:
    out = [FilterPattern(pattern=p) for p in DEFAULT_FILTER_OUT]
    out.insert(-1, FilterPattern(pattern=r"(?-i:WEB-DL|WEB\.(?:x264|H264)|HDTV\.(?:x264|XviD))",
                                 source="debrid"))
    return out


class QualityConfig(BaseModel):
    """One "version" in cli_debrid 1.x terms: hard filters + how results are scored."""
    name: str = "1080p"
    # Hard filters
    max_resolution: Literal["2160p", "1080p", "720p", "SD"] = "1080p"
    resolution_wanted: Literal["<=", "==", ">="] = "=="
    enable_hdr: bool = False
    min_size_gb: float = 0.4          # per movie / per episode
    max_size_gb: float = 0.0          # 0 = no limit
    min_bitrate_mbps: float = 3.0
    max_bitrate_mbps: float = 18.0    # 0 = no limit
    similarity_threshold: float = 0.75
    filter_in: list[FilterPattern] = Field(default_factory=list)
    filter_out: list[FilterPattern] = Field(default_factory=_default_filter_out)
    # Scoring (same formula and weights as 1.x)
    preferred_filter_in: list[WeightedPattern] = Field(default_factory=list)
    preferred_filter_out: list[WeightedPattern] = Field(
        default_factory=lambda: [WeightedPattern(pattern=r"(?i)audio.?description", weight=500)])
    resolution_weight: float = 1
    hdr_weight: float = 0
    similarity_weight: float = 1
    size_weight: float = 1
    bitrate_weight: float = 5
    year_match_weight: float = 3
    language: str = "en"
    language_weight: float = 3
    foreign_language_penalty: float = 100
    audio_description_penalty: float = 10000
    # Used when this version finds nothing acceptable (1.x "fallback version").
    fallback: "QualityConfig | None" = None

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, d):
        """Accept settings saved by earlier 2.0 builds (preset / must / never words)."""
        if not isinstance(d, dict):
            return d
        d = dict(d)
        preset = d.pop("preset", None)
        if preset and "max_resolution" not in d:
            d["max_resolution"], d["resolution_wanted"] = {
                "1080p": ("1080p", "<="), "4k": ("2160p", "=="), "4k_1080p": ("2160p", "<=")}.get(
                    preset, ("1080p", "=="))
        must = d.pop("must_contain", None)
        never = d.pop("never_contain", None)
        if must and "filter_in" not in d:
            d["filter_in"] = [{"pattern": f'"{w}"'} for w in must]
        if never and "filter_out" not in d:
            d["filter_out"] = [p.model_dump() for p in _default_filter_out()] + [
                {"pattern": f'"{w}"'} for w in never]
        for key in ("filter_in", "filter_out"):
            if isinstance(d.get(key), list):
                d[key] = [{"pattern": x} if isinstance(x, str) else x for x in d[key]]
        for key in ("preferred_filter_in", "preferred_filter_out"):
            if isinstance(d.get(key), list):
                d[key] = [{"pattern": x[0], "weight": x[1]} if isinstance(x, (list, tuple)) else x
                          for x in d[key]]
        for key in ("max_size_gb", "max_bitrate_mbps", "min_size_gb", "min_bitrate_mbps"):
            if d.get(key) is None and key in d:
                d[key] = 0.0
        return d


def v1_fallback_720() -> QualityConfig:
    """cli_debrid 1.x "720 and below", the 1080p version's fallback."""
    return QualityConfig(name="720 and below", max_resolution="720p", resolution_wanted="<=",
                         min_size_gb=0.4, min_bitrate_mbps=0, max_bitrate_mbps=18)


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
    quality: QualityConfig = Field(default_factory=lambda: QualityConfig(fallback=v1_fallback_720()))
    upgrade_days: int = 7
    notifications: list[NotifyTarget] = Field(default_factory=list)
    notify_on: NotifyOn = Field(default_factory=NotifyOn)
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
