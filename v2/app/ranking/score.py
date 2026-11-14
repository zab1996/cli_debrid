"""Scoring: higher is better. Fixed weights; no user-facing knobs."""
from __future__ import annotations

from ..config import QualityConfig
from . import Query, Release
from .filter import _episodes_in, check

RES_SCORE = {"2160p": 40, "1440p": 32, "1080p": 30, "720p": 15, "576p": 5, "480p": 5}
SOURCE_SCORE = {
    "BluRay REMUX": 12, "REMUX": 12, "BluRay": 10, "WEB-DL": 9, "WEB": 8,
    "WEBRip": 6, "HDTV": 3, "DVD": 1, "DVDRip": 1,
}


def score(r: Release, q: Query, prof: QualityConfig) -> float:
    p = r.parsed
    s = 0.0
    s += RES_SCORE.get(p.get("resolution") or "", 10)
    s += SOURCE_SCORE.get(p.get("quality") or "", 4)
    if p.get("hdr") and prof.preset != "1080p":
        s += 5
    if p.get("codec") == "hevc":
        s += 1
    audio = " ".join(p.get("audio") or []).lower()
    if "atmos" in audio or "truehd" in audio or "dts-hd" in audio:
        s += 2
    if p.get("proper") or p.get("repack"):
        s += 1
    s += 10 * (p.get("_similarity", 1.0) - 0.8)

    # Size: prefer reasonably sized files, gently, up to a cap per resolution.
    n = _episodes_in(r, q)
    per = r.size_gb / n if r.size_gb and n else 0.0
    cap = 40.0 if p.get("resolution") == "2160p" else 12.0
    if q.kind != "movie":
        cap /= 4
    s += 6 * min(per, cap) / cap

    # A whole-season pack when we asked for a season is worth a lot.
    if q.kind == "season" and not p.get("episodes") and not p.get("_unscoped"):
        s += 5
    if p.get("_unscoped"):
        s -= 8
    if r.protocol == "torrent" and r.seeders:
        s += min(r.seeders, 100) / 100
    if r.cached:
        s += 20
    return round(s, 3)


def rank(releases: list[Release], q: Query, prof: QualityConfig,
         tried: set[str] | None = None) -> list[Release]:
    """Filter and sort. Rejected releases keep their reason in `.reject`."""
    seen: set[str] = set()
    kept: list[Release] = []
    for r in releases:
        if r.key in seen:
            continue
        seen.add(r.key)
        r.reject = check(r, q, prof, tried)
        if r.reject:
            continue
        r.score = score(r, q, prof)
        kept.append(r)
    kept.sort(key=lambda r: r.score, reverse=True)
    return kept
