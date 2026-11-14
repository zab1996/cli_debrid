"""Release parsing, filtering and scoring."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Release:
    title: str
    protocol: str                 # "torrent" | "nzb"
    indexer: str                  # where it came from
    size_gb: float = 0.0
    seeders: int = 0
    info_hash: str = ""           # torrents
    magnet: str = ""              # torrents
    nzb_url: str = ""             # usenet
    guid: str = ""                # usenet
    indexer_imdb: str = ""        # imdb id the indexer tagged it with, if any
    parsed: dict = field(default_factory=dict)
    score: float = 0.0
    reject: str = ""              # why it was filtered out ("" = kept)
    cached: bool | None = None    # debrid cache status, filled at download time

    @property
    def key(self) -> str:
        """Stable identity used to remember which releases were tried."""
        return (self.info_hash or self.guid or self.nzb_url or self.title).lower()


@dataclass
class Query:
    kind: str                     # "movie" | "episode" | "season"
    title: str
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    season: int | None = None
    episode: int | None = None
    absolute: int | None = None
    aliases: list[str] = field(default_factory=list)
    is_anime: bool = False
    episode_count: int = 1        # for season packs: episodes the pack must cover
