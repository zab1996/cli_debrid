"""TMDB: search, movie details with release gating, show seasons/episodes."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from .. import config, http
from ..db import DB
from .cache import DAY, HOUR, cached

BASE = "https://api.themoviedb.org/3"
IMAGE = "https://image.tmdb.org/t/p/w342"

# Release types in /movie/{id}/release_dates
DIGITAL, PHYSICAL, THEATRICAL = 4, 5, 3
# If TMDB has no digital/physical date, assume home release this long after cinemas.
THEATRICAL_TO_HOME_DAYS = 75


class TMDBError(Exception):
    pass


async def _get(path: str, **params: Any) -> Any:
    key = config.get().tmdb_api_key
    if not key:
        raise TMDBError("TMDB API key is not set")
    # v4 read tokens are long JWTs; v3 keys are 32 hex chars.
    headers = {}
    if len(key) > 40:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    return await http.get_json(f"{BASE}{path}", params=params, headers=headers)


def _year(d: str | None) -> int | None:
    return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None


async def search(query: str, kind: str | None = None) -> list[dict]:
    """kind: 'movie', 'show' or None for both."""
    data = await _get("/search/multi", query=query, include_adult="false")
    out = []
    for r in data.get("results", []):
        mt = r.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        k = "movie" if mt == "movie" else "show"
        if kind and k != kind:
            continue
        d = r.get("release_date") if k == "movie" else r.get("first_air_date")
        out.append({
            "kind": k,
            "tmdb_id": r["id"],
            "title": r.get("title") or r.get("name") or "",
            "year": _year(d),
            "overview": r.get("overview", ""),
            "poster": f"{IMAGE}{r['poster_path']}" if r.get("poster_path") else None,
        })
    return out


def home_release_date(release_dates: dict, fallback: str | None) -> str | None:
    """Earliest digital/physical date anywhere; else theatrical + offset."""
    home: list[str] = []
    theatrical: list[str] = []
    for country in release_dates.get("results", []):
        for rd in country.get("release_dates", []):
            d = (rd.get("release_date") or "")[:10]
            if not d:
                continue
            if rd.get("type") in (DIGITAL, PHYSICAL):
                home.append(d)
            elif rd.get("type") == THEATRICAL:
                theatrical.append(d)
    if home:
        return min(home)
    base = min(theatrical) if theatrical else fallback
    if not base:
        return None
    return (date.fromisoformat(base[:10]) + timedelta(days=THEATRICAL_TO_HOME_DAYS)).isoformat()


async def movie(db: DB, tmdb_id: int) -> dict:
    async def fetch():
        m = await _get(f"/movie/{tmdb_id}", append_to_response="release_dates,external_ids")
        return {
            "tmdb_id": tmdb_id,
            "title": m.get("title", ""),
            "original_title": m.get("original_title", ""),
            "original_language": m.get("original_language", ""),
            "year": _year(m.get("release_date")),
            "imdb_id": m.get("imdb_id") or (m.get("external_ids") or {}).get("imdb_id"),
            "runtime": m.get("runtime") or 0,
            "poster": f"{IMAGE}{m['poster_path']}" if m.get("poster_path") else None,
            "release_date": home_release_date(m.get("release_dates") or {}, m.get("release_date")),
        }
    return await cached(db, f"tmdb:movie:{tmdb_id}", DAY, fetch)


async def show(db: DB, tmdb_id: int) -> dict:
    """Show info with episodes: {seasons: {"1": [{"episode", "air_date", "title"}]}}."""
    async def fetch():
        s = await _get(f"/tv/{tmdb_id}", append_to_response="external_ids,alternative_titles")
        ext = s.get("external_ids") or {}
        season_nums = [x["season_number"] for x in s.get("seasons", [])
                       if x.get("season_number", 0) > 0]

        async def one(n: int) -> tuple[int, list[dict]]:
            sd = await _get(f"/tv/{tmdb_id}/season/{n}")
            return n, [{"episode": e["episode_number"], "air_date": e.get("air_date"),
                        "title": e.get("name", "")} for e in sd.get("episodes", [])]

        seasons = dict(await asyncio.gather(*(one(n) for n in season_nums)))
        aliases = [t.get("title") for t in (s.get("alternative_titles") or {}).get("results", [])
                   if t.get("title")]
        return {
            "tmdb_id": tmdb_id,
            "title": s.get("name", ""),
            "original_title": s.get("original_name", ""),
            "original_language": s.get("original_language", ""),
            "year": _year(s.get("first_air_date")),
            "imdb_id": ext.get("imdb_id"),
            "tvdb_id": ext.get("tvdb_id"),
            "status": s.get("status", ""),
            "poster": f"{IMAGE}{s['poster_path']}" if s.get("poster_path") else None,
            "is_anime": 16 in [g.get("id") for g in s.get("genres", [])]
                        and s.get("original_language") == "ja",
            "aliases": aliases[:20],
            "seasons": {str(k): v for k, v in seasons.items()},
        }
    return await cached(db, f"tmdb:show:{tmdb_id}", 12 * HOUR, fetch)


async def find_by_imdb(db: DB, imdb_id: str) -> dict | None:
    """IMDb id -> {'kind', 'tmdb_id'}; used by list sources."""
    async def fetch():
        d = await _get(f"/find/{imdb_id}", external_source="imdb_id")
        if d.get("movie_results"):
            return {"kind": "movie", "tmdb_id": d["movie_results"][0]["id"]}
        if d.get("tv_results"):
            return {"kind": "show", "tmdb_id": d["tv_results"][0]["id"]}
        return {}
    r = await cached(db, f"tmdb:find:{imdb_id}", 30 * DAY, fetch)
    return r or None


async def find_by_tvdb(db: DB, tvdb_id: int) -> dict | None:
    async def fetch():
        d = await _get(f"/find/{tvdb_id}", external_source="tvdb_id")
        if d.get("tv_results"):
            return {"kind": "show", "tmdb_id": d["tv_results"][0]["id"]}
        if d.get("movie_results"):
            return {"kind": "movie", "tmdb_id": d["movie_results"][0]["id"]}
        return {}
    r = await cached(db, f"tmdb:findtvdb:{tvdb_id}", 30 * DAY, fetch)
    return r or None


BACKDROP = "https://image.tmdb.org/t/p/w1280"

LISTS = {
    "trending_movies": ("/trending/movie/week", "movie"),
    "trending_shows": ("/trending/tv/week", "show"),
    "popular_movies": ("/movie/popular", "movie"),
    "popular_shows": ("/tv/popular", "show"),
    "upcoming_movies": ("/movie/upcoming", "movie"),
    "on_the_air": ("/tv/on_the_air", "show"),
}


def _card(r: dict, kind: str) -> dict:
    d = r.get("release_date") if kind == "movie" else r.get("first_air_date")
    return {
        "kind": kind, "tmdb_id": r["id"], "title": r.get("title") or r.get("name") or "",
        "year": _year(d), "date": d or "", "overview": r.get("overview", ""),
        "rating": round(r.get("vote_average") or 0, 1),
        "poster": f"{IMAGE}{r['poster_path']}" if r.get("poster_path") else None,
    }


async def browse(db: DB, name: str, page: int = 1) -> list[dict]:
    """One of the Discover rows (trending, popular, upcoming, on the air)."""
    path, kind = LISTS[name]

    async def fetch():
        d = await _get(path, page=page)
        return [_card(r, kind) for r in d.get("results", [])]
    return await cached(db, f"tmdb:list:{name}:{page}", 6 * HOUR, fetch)


async def details(db: DB, kind: str, tmdb_id: int) -> dict:
    """What the Discover details page shows (overview, backdrop, genres, seasons...)."""
    path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"

    async def fetch():
        d = await _get(path, append_to_response="credits")
        cast = [c.get("name") for c in (d.get("credits") or {}).get("cast", [])[:8] if c.get("name")]
        out = _card(d, kind)
        out.update({
            "backdrop": f"{BACKDROP}{d['backdrop_path']}" if d.get("backdrop_path") else None,
            "tagline": d.get("tagline") or "",
            "genres": [g["name"] for g in d.get("genres", [])],
            "runtime": d.get("runtime") or next(iter(d.get("episode_run_time") or []), 0),
            "status": d.get("status", ""),
            "cast": cast,
            "seasons": [{"number": s["season_number"], "name": s.get("name", ""),
                         "episodes": s.get("episode_count") or 0, "air_date": s.get("air_date") or "",
                         "poster": f"{IMAGE}{s['poster_path']}" if s.get("poster_path") else None}
                        for s in d.get("seasons", []) if s.get("season_number", 0) > 0],
            "networks": [n["name"] for n in d.get("networks", [])],
        })
        return out
    return await cached(db, f"tmdb:details:{kind}:{tmdb_id}", DAY, fetch)
