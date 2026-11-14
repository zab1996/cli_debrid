from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    WANTED = "Wanted"            # known, ready to search
    SEARCHING = "Searching"      # being searched / picking a release
    DOWNLOADING = "Downloading"  # handed to the engine, waiting for files
    DONE = "Done"                # in the library
    WAITING = "Waiting"          # unreleased or retry later (next_attempt_at)
    FAILED = "Failed"            # gave up; retried weekly


# Backoff for Waiting retries after a search finds nothing (seconds).
RETRY_BACKOFF = [3600, 6 * 3600, 24 * 3600]
DAILY = 24 * 3600
WEEKLY = 7 * DAILY
# After this many empty searches an item goes to Failed.
MAX_EMPTY_SEARCHES = 10


def next_backoff(attempts: int) -> int:
    """Seconds to wait after `attempts` empty searches (1-based)."""
    if attempts <= 0:
        return 0
    if attempts <= len(RETRY_BACKOFF):
        return RETRY_BACKOFF[attempts - 1]
    return DAILY


@dataclass
class Item:
    id: int
    kind: str              # "movie" | "episode"
    tmdb_id: int           # for episodes: the show's TMDB id
    tvdb_id: int | None
    imdb_id: str | None
    title: str             # movie title, or show title for episodes
    year: int | None
    season: int | None
    episode: int | None
    absolute: int | None
    state: State
    release_date: str | None = None
    next_attempt_at: float | None = None
    attempts: int = 0
    release_name: str | None = None
    engine_id: str | None = None
    prev_engine_id: str | None = None
    mode: str | None = None
    downloading_since: float | None = None
    file_path: str | None = None
    done_at: float | None = None
    source: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "episode":
            return f"{self.title} S{self.season:02d}E{self.episode:02d}"
        return f"{self.title} ({self.year})" if self.year else self.title
