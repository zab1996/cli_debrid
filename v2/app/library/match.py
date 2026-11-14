"""Which file in a download is which movie/episode."""
from __future__ import annotations

from ..downloads.engine import EngineFile
from ..models import Item
from ..ranking.parse import is_obfuscated, parse

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".webm"}
MIN_VIDEO_BYTES = 50 * 1024 * 1024


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def videos(files: list[EngineFile]) -> list[EngineFile]:
    out = []
    for f in files:
        base = f.name.rsplit("/", 1)[-1].lower()
        if _ext(base) not in VIDEO_EXT:
            continue
        if "sample" in base.split(".")[0] or "/sample" in f.name.lower():
            continue
        if f.size and f.size < MIN_VIDEO_BYTES:
            continue
        out.append(f)
    return out


def for_movie(files: list[EngineFile]) -> EngineFile | None:
    vids = videos(files)
    return max(vids, key=lambda f: f.size) if vids else None


def for_episodes(files: list[EngineFile], items: list[Item]) -> dict[int, EngineFile]:
    """Map item id -> file. Unmatched items are left out."""
    vids = videos(files)
    out: dict[int, EngineFile] = {}
    if len(items) == 1 and len(vids) == 1:
        return {items[0].id: vids[0]}
    parsed = []
    for f in vids:
        base = f.name.rsplit("/", 1)[-1]
        if is_obfuscated(base):
            continue
        p = parse(base)
        seasons = p["seasons"]
        if not seasons:
            # "Season 2/Show - 05.mkv": take the season from the folder.
            folder = parse(f.name.rsplit("/", 1)[0]) if "/" in f.name else {"seasons": []}
            seasons = folder["seasons"]
        parsed.append((f, seasons, p["episodes"]))
    for it in items:
        hits = [f for f, s, e in parsed if it.season in s and it.episode in e]
        if not hits:
            hits = [f for f, s, e in parsed
                    if not s and ((it.absolute and it.absolute in e)
                                  or (it.season == 1 and it.episode in e))]
        if hits:
            out[it.id] = max(hits, key=lambda f: f.size)
    return out
