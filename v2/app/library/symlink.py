"""Symlink placement with fixed, Plex/Jellyfin-friendly naming.

Movies:  <movies>/Title (Year) {tmdb-603}/Title (Year).mkv
Shows:   <shows>/Title (Year) {tmdb-1396}/Season 01/Title (Year) - S01E02.mkv
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import LibraryConfig
from ..models import Item

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(name: str) -> str:
    name = _BAD.sub("", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name) or "Unknown"


def _base(item: Item) -> str:
    return clean(f"{item.title} ({item.year})" if item.year else item.title)


def folder_for(item: Item, lib: LibraryConfig) -> Path:
    root = lib.movies_path if item.kind == "movie" else lib.shows_path
    return Path(root) / f"{_base(item)} {{tmdb-{item.tmdb_id}}}"


def link_path(item: Item, lib: LibraryConfig, source: str) -> Path:
    ext = os.path.splitext(source)[1].lower() or ".mkv"
    if item.kind == "movie":
        return folder_for(item, lib) / f"{_base(item)}{ext}"
    season = f"Season {item.season:02d}"
    return folder_for(item, lib) / season / f"{_base(item)} - S{item.season:02d}E{item.episode:02d}{ext}"


def place(item: Item, lib: LibraryConfig, source: str) -> Path:
    """Create (or repoint) the symlink for `item` to `source`. Returns the link path."""
    dest = link_path(item, lib, source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Remove an older link for the same item with a different extension.
    stem = dest.stem
    for other in dest.parent.glob(f"{glob_escape(stem)}.*"):
        if other.is_symlink() and other != dest:
            other.unlink()
    tmp = dest.with_name(dest.name + ".tmp-link")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(source, tmp)
    os.replace(tmp, dest)
    return dest


def remove(path: str | None) -> None:
    if path and os.path.islink(path):
        os.unlink(path)
        _prune_empty(Path(path).parent)


def _prune_empty(d: Path, depth: int = 2) -> None:
    for _ in range(depth):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)
