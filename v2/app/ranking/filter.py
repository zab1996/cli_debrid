"""Hard filters: a release either fits the request and profile or it doesn't."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..config import QualityConfig
from . import Query, Release
from .parse import is_obfuscated, normalize, parse

TITLE_MATCH = 0.82

PRESET_RES = {
    "1080p": {"1080p", "720p"},
    "4k": {"2160p"},
    "4k_1080p": {"2160p", "1080p"},
}
NZB_MAX_EPISODES = 2   # double episodes are fine; anything bigger is a pack
TRASH_QUALITY = {"CAM", "TeleSync", "TeleCine", "SCR", "Screener", "TS", "TC"}


_MOVIE_SUFFIX = " the movie"
_COUNTRY_TAGS = {"us", "uk", "au", "nz", "ca", "ie"}


def title_similarity(release_title: str, wanted: list[str]) -> float:
    rt = normalize(release_title)
    if not rt:
        return 0.0
    if rt.endswith(_MOVIE_SUFFIX):
        rt = rt[: -len(_MOVIE_SUFFIX)]
    best = 0.0
    for w in wanted:
        wn = re.sub(r" (19|20)\d\d$", "", normalize(w))  # "WAR (2026)" -> "war"
        if wn.endswith(_MOVIE_SUFFIX):
            wn = wn[: -len(_MOVIE_SUFFIX)]
        if not wn:
            continue
        if rt == wn:
            return 1.0
        best = max(best, SequenceMatcher(None, rt, wn).ratio())
        # "the office us" vs "the office": allow a trailing country tag only.
        if rt.startswith(wn + " ") and rt[len(wn) + 1:] in _COUNTRY_TAGS:
            best = max(best, 0.9)
    return best


def _words_hit(text: str, words: list[str]) -> str | None:
    low = text.lower()
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w.startswith("/") and w.endswith("/") and len(w) > 2:
            if re.search(w[1:-1], text, re.IGNORECASE):
                return w
        elif re.search(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", low):
            return w
    return None


def check(r: Release, q: Query, prof: QualityConfig, tried: set[str] | None = None) -> str:
    """Return "" if the release is acceptable, else a short reason."""
    if tried and r.key in tried:
        return "already tried"
    if is_obfuscated(r.title):
        return "obfuscated name"
    p = r.parsed or parse(r.title)
    r.parsed = p

    # --- right title -------------------------------------------------------
    names = [q.title, *q.aliases]
    sim = title_similarity(p.get("title", ""), names)
    p["_similarity"] = sim
    if sim < TITLE_MATCH:
        # ID-searched results tagged with our imdb id are trusted.
        if not (q.imdb_id and r.indexer_imdb and r.indexer_imdb.lstrip("t0") == q.imdb_id.lstrip("t0")):
            return f"title mismatch ({sim:.2f})"
    if q.imdb_id and r.indexer_imdb and r.indexer_imdb.lstrip("t0") != q.imdb_id.lstrip("t0"):
        return "different imdb id"

    # --- right movie / episode --------------------------------------------
    if q.kind == "movie":
        if p["seasons"] or p["episodes"]:
            return "looks like a show"
        if q.year and p.get("year") and abs(p["year"] - q.year) > 1:
            return f"year {p['year']}"
    else:
        seasons, eps = p["seasons"], p["episodes"]
        if not seasons and eps:
            # "[Group] Show - 112" or "Show - 01 ~ 24": no season tag. The
            # numbers are absolute, or season-1 numbers for single-season shows.
            if q.kind == "episode":
                if not ((q.absolute and q.absolute in eps) or (q.season == 1 and q.episode in eps)):
                    return "wrong episode"
            elif q.season != 1 and not q.absolute:
                return "no season"
            elif len(eps) < q.episode_count:
                return "partial pack"
        elif not seasons:
            # Bare "Show (1997)" / "Show COMPLETE": maybe a whole-series pack.
            # Allowed with a score penalty; the file list is checked after adding.
            p["_unscoped"] = True
        elif q.season not in seasons:
            return "wrong season"
        elif q.kind == "episode" and eps and q.episode not in eps:
            return "wrong episode"
        elif q.kind == "season" and eps and len(eps) < q.episode_count:
            # A pack must not be a single episode or a partial range.
            return "partial pack"

    # --- no usenet season packs ----------------------------------------------
    # NZB packs are slow to process and stream badly; usenet must be one
    # episode per release (a double episode like E01E02 is fine). Packs are
    # still allowed for debrid torrents.
    if r.protocol == "nzb" and q.kind != "movie":
        eps = p["episodes"]
        if q.kind == "season" or not eps or len(eps) > NZB_MAX_EPISODES:
            return "usenet pack"

    # --- quality ------------------------------------------------------------
    if p.get("trash") or p.get("quality") in TRASH_QUALITY:
        return "cam/screener"
    if p.get("3d"):
        return "3D"
    res = p.get("resolution")
    allowed = PRESET_RES[prof.preset]
    if res and res not in allowed:
        return f"resolution {res}"
    if not res and prof.preset == "4k":
        return "unknown resolution"

    # --- size (per episode for packs) -------------------------------------
    n = _episodes_in(r, q)
    per = r.size_gb / n if r.size_gb and n else r.size_gb
    if prof.min_size_gb and per and per < prof.min_size_gb:
        return f"too small ({per:.2f} GB)"
    if prof.max_size_gb and per > prof.max_size_gb:
        return f"too big ({per:.2f} GB)"

    # --- words and language ------------------------------------------------
    hit = _words_hit(r.title, prof.never_contain)
    if hit:
        return f"contains '{hit}'"
    if prof.must_contain and not _words_hit(r.title, prof.must_contain):
        return "missing required word"
    langs = p.get("languages") or []
    if prof.language and langs and prof.language not in langs and not p.get("dubbed"):
        return f"language {','.join(langs)}"
    return ""


def _episodes_in(r: Release, q: Query) -> int:
    """Rough number of episodes in a release, to compare per-episode size."""
    if q.kind == "movie":
        return 1
    p = r.parsed
    if p.get("episodes"):
        return len(p["episodes"])
    seasons = p.get("seasons") or [q.season]
    return max(1, q.episode_count) * max(1, len(seasons))
