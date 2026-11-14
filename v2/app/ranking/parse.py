"""Thin wrapper over PTT with the few fixes v1 learned the hard way."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from PTT import parse_title

# Obfuscated usenet names: a bare hash or long random run. PTT reads stray
# digits out of these as season/episode numbers, so don't trust their parse.
_OBF_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_OBF_ALNUM = re.compile(r"(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}")
# "[ Show.S01E05.1080p.WEB-DL-GRP ] -": PTT returns an empty title for these.
_WRAPPED = re.compile(r"^\s*[\[(]\s*(?P<inner>[^\[\]()]+?)\s*[\])][\s._-]*$")
# Spanish "Cap.105" = S01E05.
_CAP = re.compile(r"\bCap\.(\d)(\d{2})\b", re.IGNORECASE)


def is_obfuscated(name: str) -> bool:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return bool(_OBF_HEX.fullmatch(stem) or _OBF_ALNUM.fullmatch(stem))


@lru_cache(maxsize=8192)
def _parse(title: str) -> dict:
    m = _WRAPPED.match(title)
    if m:
        title = m.group("inner")
    title = _CAP.sub(lambda m: f"S{int(m.group(1)):02d}E{m.group(2)}", title)
    try:
        return parse_title(title)
    except Exception:
        return {"title": title, "seasons": [], "episodes": [], "languages": []}


def parse(title: str) -> dict:
    p = dict(_parse(title))
    res = p.get("resolution")
    if res and res.endswith("i") and res[:-1].isdigit():
        p["resolution"] = res[:-1] + "p"   # 1080i -> 1080p
    p.setdefault("seasons", [])
    p.setdefault("episodes", [])
    p.setdefault("languages", [])
    return p


def normalize(s: str) -> str:
    """Lowercase, strip accents and punctuation, '&' -> 'and', collapse spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())
