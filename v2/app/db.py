"""SQLite storage. One connection, one lock: every write is serialized.

v1 lost a lot of time to "database is locked" bursts from many writers.
v2 keeps a single connection guarded by a lock; reads share it too, which
is plenty for a single-user app.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import Item, State

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,              -- movie | episode
    tmdb_id         INTEGER NOT NULL,
    tvdb_id         INTEGER,
    imdb_id         TEXT,
    title           TEXT NOT NULL,
    year            INTEGER,
    season          INTEGER,
    episode         INTEGER,
    absolute        INTEGER,
    state           TEXT NOT NULL,
    release_date    TEXT,
    next_attempt_at REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    release_name    TEXT,
    engine_id       TEXT,
    prev_engine_id  TEXT,             -- old download kept until a replacement is ready
    mode            TEXT,             -- NULL | 'upgrade' | 'replace'
    downloading_since REAL,
    file_path       TEXT,
    source          TEXT,
    added_at        REAL NOT NULL,
    updated_at      REAL NOT NULL,
    done_at         REAL,
    UNIQUE (kind, tmdb_id, season, episode)
);
CREATE INDEX IF NOT EXISTS items_state ON items(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS items_show ON items(kind, tmdb_id);

-- One row per requested show. Episodes live in items.
CREATE TABLE IF NOT EXISTS shows (
    tmdb_id        INTEGER PRIMARY KEY,
    tvdb_id        INTEGER,
    imdb_id        TEXT,
    title          TEXT NOT NULL,
    original_title TEXT,
    year           INTEGER,
    aliases        TEXT NOT NULL DEFAULT '[]',   -- JSON list
    is_anime       INTEGER NOT NULL DEFAULT 0,
    status         TEXT,
    seasons        TEXT,                         -- JSON list, NULL = all seasons
    source         TEXT,
    added_at       REAL NOT NULL,
    refreshed_at   REAL
);

-- Every release ever picked for an item, so replace/upgrade can skip it.
CREATE TABLE IF NOT EXISTS releases_tried (
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    release_key TEXT NOT NULL,     -- info hash or NZB guid
    name        TEXT NOT NULL,
    score       REAL,
    outcome     TEXT,              -- picked | failed | replaced
    at          REAL NOT NULL,
    PRIMARY KEY (item_id, release_key)
);

-- Metadata responses (TMDB/TVDB) with expiry.
CREATE TABLE IF NOT EXISTS meta_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at REAL NOT NULL
);

-- Short activity log for the home page.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    at      REAL NOT NULL,
    item_id INTEGER,
    message TEXT NOT NULL
);

-- Every (source, title) ever seen, so each is acted on only once.
CREATE TABLE IF NOT EXISTS requests (
    source     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    tmdb_id    INTEGER NOT NULL,
    title      TEXT,
    first_seen REAL NOT NULL,
    PRIMARY KEY (source, kind, tmdb_id)
);

-- Plex "Report issue" reports already handled (Plex has no way to resolve them).
CREATE TABLE IF NOT EXISTS plex_reports (
    id         TEXT PRIMARY KEY,
    item_id    INTEGER,
    title      TEXT,
    status     TEXT NOT NULL,   -- replacing | no_match | ignored | seen
    message    TEXT,
    at         REAL NOT NULL
);

-- Titles that must never be added again (by lists or by hand).
-- season NULL = the whole movie/show; season N = just that season of a show.
CREATE TABLE IF NOT EXISTS blacklist (
    kind     TEXT NOT NULL,        -- movie | show
    tmdb_id  INTEGER NOT NULL,
    season   INTEGER NOT NULL DEFAULT -1,   -- -1 = whole title
    title    TEXT,
    reason   TEXT,
    at       REAL NOT NULL,
    PRIMARY KEY (kind, tmdb_id, season)
);

-- Releases that must never be picked again, for anything.
CREATE TABLE IF NOT EXISTS release_blacklist (
    release_key TEXT PRIMARY KEY,
    name        TEXT,
    reason      TEXT,
    at          REAL NOT NULL
);

-- Episodes the user deleted from a show they still follow: the daily show
-- refresh must not add them back. Cleared when the show is added again by hand.
CREATE TABLE IF NOT EXISTS skipped_episodes (
    tmdb_id INTEGER NOT NULL,
    season  INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, season, episode)
);

-- Outgoing notifications, batched and sent by the worker (survives restarts).
CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY,
    event   TEXT NOT NULL,         -- collected | upgraded | failed | blacklisted
    title   TEXT NOT NULL,
    detail  TEXT,
    item_id INTEGER,
    at      REAL NOT NULL,
    tries   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ITEM_COLS = ("id, kind, tmdb_id, tvdb_id, imdb_id, title, year, season, episode, absolute, "
              "state, release_date, next_attempt_at, attempts, release_name, engine_id, "
              "prev_engine_id, mode, downloading_since, file_path, done_at, source")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT OR IGNORE INTO kv(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # Columns added after the first release: (table, column, type).
    ADDED_COLUMNS = [
        ("items", "poster", "TEXT"),
        ("shows", "poster", "TEXT"),
        ("items", "runtime", "INTEGER"),
        ("shows", "runtime", "INTEGER"),
    ]

    def _migrate(self) -> None:
        for table, col, typ in self.ADDED_COLUMNS:
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- items -------------------------------------------------------------

    def add_item(self, *, kind: str, tmdb_id: int, title: str, year: int | None = None,
                 season: int | None = None, episode: int | None = None,
                 release_date: str | None = None, source: str | None = None,
                 tvdb_id: int | None = None, imdb_id: str | None = None,
                 absolute: int | None = None, state: State = State.WANTED,
                 next_attempt_at: float | None = None, poster: str | None = None,
                 runtime: int | None = None) -> int | None:
        """Insert an item. Returns its id, or None if it already exists."""
        now = time.time()
        with self.tx() as c:
            # UNIQUE treats NULLs as distinct, so check movies explicitly.
            existing = c.execute(
                "SELECT id FROM items WHERE kind=? AND tmdb_id=? AND season IS ? AND episode IS ?",
                (kind, tmdb_id, season, episode),
            ).fetchone()
            if existing:
                return None
            cur = c.execute(
                "INSERT INTO items(kind, tmdb_id, tvdb_id, imdb_id, title, year, season, episode,"
                " absolute, state, release_date, next_attempt_at, source, added_at, updated_at, poster,"
                " runtime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, tmdb_id, tvdb_id, imdb_id, title, year, season, episode, absolute,
                 state.value, release_date, next_attempt_at, source, now, now, poster, runtime),
            )
            return cur.lastrowid

    def get_item(self, item_id: int) -> Item | None:
        rows = self.query(f"SELECT {_ITEM_COLS} FROM items WHERE id=?", (item_id,))
        return _row_to_item(rows[0]) if rows else None

    def items_in(self, state: State, *, due_only: bool = False, limit: int = 100) -> list[Item]:
        if due_only:
            rows = self.query(
                f"SELECT {_ITEM_COLS} FROM items WHERE state=? AND "
                "(next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id LIMIT ?",
                (state.value, time.time(), limit),
            )
        else:
            rows = self.query(
                f"SELECT {_ITEM_COLS} FROM items WHERE state=? ORDER BY id LIMIT ?",
                (state.value, limit),
            )
        return [_row_to_item(r) for r in rows]

    def set_state(self, item_id: int, state: State, **fields: Any) -> None:
        allowed = {"next_attempt_at", "attempts", "release_name", "engine_id",
                   "file_path", "done_at", "release_date", "absolute", "prev_engine_id",
                   "mode", "downloading_since"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown item fields: {bad}")
        sets = ["state=?", "updated_at=?"] + [f"{k}=?" for k in fields]
        params = [state.value, time.time(), *fields.values(), item_id]
        with self.tx() as c:
            c.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", params)

    def record_release(self, item_id: int, key: str, name: str, score: float | None,
                       outcome: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO releases_tried(item_id, release_key, name, score, outcome, at)"
                      " VALUES (?,?,?,?,?,?) ON CONFLICT(item_id, release_key) DO UPDATE SET"
                      " outcome=excluded.outcome, at=excluded.at",
                      (item_id, key, name, score, outcome, time.time()))

    def items_by_engine(self, engine_id: str) -> list[Item]:
        rows = self.query(f"SELECT {_ITEM_COLS} FROM items WHERE engine_id=? ORDER BY id",
                          (engine_id,))
        return [_row_to_item(r) for r in rows]

    def engine_id_in_use(self, engine_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM items WHERE engine_id=? OR prev_engine_id=? LIMIT 1",
                               (engine_id, engine_id)))

    def state_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in State}
        for r in self.query("SELECT state, COUNT(*) n FROM items GROUP BY state"):
            counts[r["state"]] = r["n"]
        return counts

    # --- events ------------------------------------------------------------

    def log_event(self, message: str, item_id: int | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(at, item_id, message) VALUES (?,?,?)",
                      (time.time(), item_id, message))
            # Keep the table small.
            c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 1000")

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT at, item_id, message FROM events ORDER BY id DESC LIMIT ?",
                          (limit,))


def _row_to_item(r: sqlite3.Row) -> Item:
    d = dict(r)
    d["state"] = State(d["state"])
    return Item(**d)
