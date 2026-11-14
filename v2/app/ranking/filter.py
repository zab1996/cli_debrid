"""Hard filters: a release either fits the request and profile or it doesn't."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..config import QualityConfig
from . import Query, Release
from .parse import is_obfuscated, normalize, parse

RES_RANK = {"2160p": 4, "4k": 4, "1440p": 3, "1080p": 3, "720p": 2, "576p": 1, "480p": 1, "sd": 1}
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


def res_rank(res: str | None) -> int:
    return RES_RANK.get((res or "").lower(), 0)


def resolution_ok(res: str | None, prof: QualityConfig) -> bool:
    """1.x resolution rule: <=, == or >= the version's max resolution."""
    have, want = res_rank(res), res_rank(prof.max_resolution)
    if prof.resolution_wanted == "<=":
        return have <= want
    if prof.resolution_wanted == ">=":
        return have >= want
    return have == want


def _is_regex(p: str) -> bool:
    return any(c in p for c in "\\^$.|?*+()[]{}")


def smart_search(pattern: str, text: str) -> bool:
    """1.x matching: "quoted" = plain substring; regex if it looks like one; else substring.
    Always case-insensitive unless the pattern turns that off itself ((?-i:...))."""
    if not pattern or not text:
        return False
    if len(pattern) > 1 and pattern[0] == pattern[-1] == '"':
        return pattern[1:-1].lower() in text.lower()
    if _is_regex(pattern):
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return pattern.lower() in text.lower()
    return pattern.lower() in text.lower()


def _applies(source: str, protocol: str) -> bool:
    return source == "both" or (source == "nzb") == (protocol == "nzb")


def bitrate_mbps(size_gb: float, runtime_min: int) -> float:
    if not size_gb or not runtime_min:
        return 0.0
    return size_gb * 8 * 1024 ** 3 / (runtime_min * 60) / 1_000_000


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
    if sim < prof.similarity_threshold:
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

    # --- version filters (same rules as cli_debrid 1.x) ----------------------
    if p.get("trash") or p.get("quality") in TRASH_QUALITY:
        return "cam/screener"
    if p.get("3d"):
        return "3D"
    res = p.get("resolution")
    if not resolution_ok(res, prof):
        return f"resolution {res or 'unknown'} (want {prof.resolution_wanted} {prof.max_resolution})"
    if not prof.enable_hdr and p.get("hdr"):
        return "HDR (disabled)"

    n = _episodes_in(r, q)
    per = r.size_gb / n if r.size_gb and n else r.size_gb
    p["_per_item_gb"] = per
    if prof.min_size_gb and per < prof.min_size_gb:
        return f"too small ({per:.2f} GB, min {prof.min_size_gb})"
    if prof.max_size_gb and per > prof.max_size_gb:
        return f"too big ({per:.2f} GB, max {prof.max_size_gb})"
    br = bitrate_mbps(per, q.runtime)
    p["_bitrate"] = br
    if br:
        if prof.min_bitrate_mbps and br < prof.min_bitrate_mbps:
            return f"bitrate too low ({br:.1f} Mbps, min {prof.min_bitrate_mbps:g})"
        if prof.max_bitrate_mbps and br > prof.max_bitrate_mbps:
            return f"bitrate too high ({br:.1f} Mbps, max {prof.max_bitrate_mbps:g})"

    for f in prof.filter_out:
        if _applies(f.source, r.protocol) and smart_search(f.pattern, r.title):
            return f"filter out: {f.pattern}"
    ins = [f for f in prof.filter_in if _applies(f.source, r.protocol)]
    if ins and not any(smart_search(f.pattern, r.title) for f in ins):
        return "no filter-in match"
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
