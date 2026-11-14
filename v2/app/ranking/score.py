"""Scoring: the cli_debrid 1.x formula, driven by the version's weights.

total = similarity*10*w + resolution_rank*50*w + hdr*10*w + size(0-20)*w
      + bitrate(0-20)*w + year(0/2.5/5)*w + preferred filters
      - foreign language / audio description penalties
      + indexer priority (+ a debrid-cached nudge, + pack bonus / penalty)
"""
from __future__ import annotations

import re

from ..config import QualityConfig
from . import Query, Release
from .filter import check, res_rank, smart_search

# Where size/bitrate stop earning points when the version sets no maximum (1.x values).
SIZE_CAP_GB = {"movie": 30.0, "episode": 3.0}
BITRATE_CAP = {4: 25.0, 3: 12.0, 2: 6.0}          # by resolution rank; SD/unknown -> 3
PACK_BONUS = 30            # asked for a season and got a full-season pack
PACK_PENALTY = -500        # asked for one episode and got a pack (1.x does the same)
CACHED_BONUS = 20
_AUDIO_DESC = re.compile(r"(?i)audio.?desc|\bdescriptive\b")
_MULTI = re.compile(r"(?i)\b(multi|dual)\b")


def _scaled(value: float, low: float, high: float) -> float:
    """1.x curve: (clamped fraction ** 1.5) * 20."""
    if value < low or high <= low:
        return 0.0
    f = max(0.0, min((value - low) / (high - low), 1.0))
    return (f ** 1.5) * 20.0


def breakdown(r: Release, q: Query, prof: QualityConfig, priority: float = 0.0) -> dict[str, float]:
    p = r.parsed
    rank_ = res_rank(p.get("resolution"))
    kind = "movie" if q.kind == "movie" else "episode"
    per = p.get("_per_item_gb", r.size_gb)
    size_high = prof.max_size_gb or max(SIZE_CAP_GB[kind], prof.min_size_gb * 2 or 1)
    br = p.get("_bitrate", 0.0)
    br_high = prof.max_bitrate_mbps or max(BITRATE_CAP.get(rank_, 3.0), prof.min_bitrate_mbps * 2 or 1)
    year = 0.0
    if q.kind == "movie" and q.year and p.get("year"):
        year = 5.0 if p["year"] == q.year else 2.5 if abs(p["year"] - q.year) <= 1 else 0.0

    out = {
        "similarity": p.get("_similarity", 1.0) * 10 * prof.similarity_weight,
        "resolution": rank_ * 50 * prof.resolution_weight,
        "hdr": (10 if p.get("hdr") else 0) * prof.hdr_weight,
        "size": _scaled(per, prof.min_size_gb, size_high) * prof.size_weight,
        "bitrate": _scaled(br, prof.min_bitrate_mbps, br_high) * prof.bitrate_weight if br else 0.0,
        "year": year * prof.year_match_weight,
        "preferred": 0.0,
        "language": 0.0,
        "audio_description": 0.0,
        "priority": priority,
        "pack": 0.0,
        "cached": CACHED_BONUS if r.cached else 0.0,
    }
    for f in prof.preferred_filter_in:
        if smart_search(f.pattern, r.title):
            out["preferred"] += f.weight
    for f in prof.preferred_filter_out:
        if smart_search(f.pattern, r.title):
            out["preferred"] -= f.weight
    langs = p.get("languages") or []
    if prof.language and langs and prof.language not in langs and not (p.get("dubbed") or _MULTI.search(r.title)):
        out["language"] = -prof.foreign_language_penalty * prof.language_weight
    if prof.audio_description_penalty and _AUDIO_DESC.search(r.title):
        out["audio_description"] = -prof.audio_description_penalty
    if q.kind != "movie":
        eps = p.get("episodes") or []
        is_pack = bool(p.get("_unscoped")) or not eps or len(eps) > 2
        if q.kind == "season" and is_pack:
            out["pack"] = PACK_BONUS
        elif q.kind == "episode" and is_pack:
            out["pack"] = PACK_PENALTY
    if r.protocol == "torrent" and r.seeders:
        out["priority"] += min(r.seeders, 100) / 100
    return out


def score(r: Release, q: Query, prof: QualityConfig, priority: float = 0.0) -> float:
    return round(sum(breakdown(r, q, prof, priority).values()), 3)


def _priorities() -> dict[str, float]:
    """Indexer label -> priority from settings (1.x per-scraper priority)."""
    from .. import config
    from ..scrapers import label
    out: dict[str, float] = {}
    for i in config.get().indexers:
        if i.priority:
            out[label(i)] = i.priority
            out.setdefault(i.type, i.priority)
    return out


def priority_for(r: Release, prios: dict[str, float] | None = None) -> float:
    prios = _priorities() if prios is None else prios
    if not prios:
        return 0.0
    if r.indexer in prios:
        return prios[r.indexer]
    return prios.get(r.indexer.split(":")[0], 0.0)


def rank(releases: list[Release], q: Query, prof: QualityConfig,
         tried: set[str] | None = None) -> list[Release]:
    """Filter and sort with the version; if nothing passes, use its fallback version
    (1.x "fallback version"). Rejected releases keep their reason in `.reject`."""
    prios = _priorities()
    seen: set[str] = set()
    unique = []
    for r in releases:
        if r.key not in seen:
            seen.add(r.key)
            unique.append(r)
    for version in (prof, prof.fallback):
        if version is None:
            continue
        kept: list[Release] = []
        for r in unique:
            r.reject = check(r, q, version, tried)
            if r.reject:
                continue
            r.score = score(r, q, version, priority_for(r, prios))
            if version is not prof:
                r.parsed["_version"] = version.name
            kept.append(r)
        if kept:
            kept.sort(key=lambda r: r.score, reverse=True)
            return kept
    return []
