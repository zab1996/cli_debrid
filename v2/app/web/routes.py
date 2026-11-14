from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from urllib.parse import quote, urlencode
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import __version__, blacklist, catalog, checks, config, interactive, manage, notify
from ..config import Config
from ..downloads import engine
from ..metadata import tmdb
from ..models import State

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__

router = APIRouter()

INDEXER_TYPES = ["torrentio", "zilean", "jackett", "prowlarr", "newznab"]
SOURCE_TYPES = {
    "plex_watchlist": "My Plex watchlist",
    "trakt_list": "Trakt list (user/list)",
    "trakt_watchlist": "Trakt watchlist (username)",
    "mdblist": "MDBList list (URL)",
}
DEBRIDS = {"": "None", "realdebrid": "Real-Debrid", "torbox": "TorBox", "alldebrid": "AllDebrid",
           "premiumize": "Premiumize", "debridlink": "Debrid-Link"}
PRESETS = {"1080p": "1080p (720p fallback)", "4k": "4K only", "4k_1080p": "4K, else 1080p"}


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 0:
        s = -s
        for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
            if s >= n:
                return f"in {s // n}{unit}"
        return "soon"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"


templates.env.filters["ago"] = _ago
templates.env.filters["ymd"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _duration(seconds: float) -> str:
    s = int(seconds or 0)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


templates.env.filters["duration"] = _duration


def render(request: Request, name: str, **ctx):
    ctx.setdefault("nav", name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ home


@router.get("/health")
def health(request: Request) -> dict:
    worker = request.app.state.worker
    return {
        "ok": True,
        "version": __version__,
        "steps": [{"name": s.name, "runs": s.runs, "error": s.last_error} for s in worker.steps],
    }


async def engine_summary(request: Request) -> dict:
    proc = request.app.state.engine
    out = {"running": proc.running, "available": proc.available, "restarts": proc.restarts}
    if proc.running:
        try:
            out.update(await asyncio.wait_for(engine.status(), 3))
            out["reachable"] = True
        except Exception:
            out["reachable"] = False
    return out


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not config.configured(config.get()):
        return back("/setup")
    db = request.app.state.db
    problems = [f"{s.name}: {s.last_error}" for s in request.app.state.worker.steps if s.last_error]
    today = time.strftime("%Y-%m-%d")
    week_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    soon = time.strftime("%Y-%m-%d", time.localtime(time.time() + 14 * 86400))
    recent_movies = db.query(
        "SELECT id, title, year, poster, release_name, done_at FROM items WHERE kind='movie'"
        " AND state='Done' ORDER BY done_at DESC LIMIT 12")
    recent_shows = db.query(
        "SELECT s.tmdb_id, s.title, s.year, s.poster, MAX(i.done_at) last_done, COUNT(i.id) eps,"
        " (SELECT season || 'x' || episode FROM items j WHERE j.kind='episode' AND j.tmdb_id=s.tmdb_id"
        "  AND j.state='Done' ORDER BY j.done_at DESC LIMIT 1) latest"
        " FROM shows s JOIN items i ON i.kind='episode' AND i.tmdb_id=s.tmdb_id AND i.state='Done'"
        " GROUP BY s.tmdb_id ORDER BY last_done DESC LIMIT 12")
    aired = db.query(
        "SELECT id, title, season, episode, release_date, state FROM items WHERE kind='episode'"
        " AND release_date BETWEEN ? AND ? ORDER BY release_date DESC, title LIMIT 60",
        (week_ago, today))
    airing_soon = db.query(
        "SELECT id, title, season, episode, release_date FROM items WHERE kind='episode'"
        " AND release_date >= ? AND release_date <= ? AND state != 'Done'"
        " ORDER BY release_date, title, season, episode LIMIT 80", (today, soon))
    movies_until = time.strftime("%Y-%m-%d", time.localtime(time.time() + 120 * 86400))
    upcoming_movies = db.query(
        "SELECT id, title, year, release_date, state FROM items WHERE kind='movie'"
        " AND state != 'Done' AND release_date >= ? AND release_date <= ?"
        " ORDER BY release_date, title LIMIT 60", (week_ago, movies_until))
    library = {
        "movies": db.query("SELECT COUNT(*) n FROM items WHERE kind='movie' AND state='Done'")[0]["n"],
        "shows": db.query("SELECT COUNT(*) n FROM shows")[0]["n"],
        "episodes": db.query("SELECT COUNT(*) n FROM items WHERE kind='episode' AND state='Done'")[0]["n"],
    }
    return render(request, "home.html", counts=db.state_counts(), events=db.recent_events(40),
                  engine=await engine_summary(request), problems=problems,
                  recent_movies=recent_movies, recent_shows=recent_shows, aired=aired,
                  airing_soon=airing_soon, upcoming_movies=upcoming_movies, library=library,
                  today=today,
                  uptime=time.time() - request.app.state.started_at)


QUEUE_ORDER = [State.SEARCHING, State.DOWNLOADING, State.WANTED, State.WAITING, State.FAILED,
               State.DONE]


@router.get("/queues", response_class=HTMLResponse)
def queues(request: Request):
    db = request.app.state.db
    sections = []
    for st in QUEUE_ORDER:
        order = "done_at DESC" if st is State.DONE else (
            "next_attempt_at" if st in (State.WAITING, State.FAILED) else "updated_at DESC")
        rows = db.query(
            "SELECT id, kind, title, year, season, episode, release_date, release_name, attempts,"
            " next_attempt_at, added_at, updated_at, done_at, mode, source FROM items"
            f" WHERE state=? ORDER BY {order} LIMIT 300", (st.value,))
        total = db.query("SELECT COUNT(*) n FROM items WHERE state=?", (st.value,))[0]["n"]
        sections.append({"name": st.value, "items": rows, "total": total})
    return render(request, "queues.html", sections=sections)


# ------------------------------------------------------------------ library


LIB_SORTS = {"title_asc": ("title", False), "title_desc": ("title", True),
             "added_desc": ("added", True), "year_desc": ("year", True), "updated_desc": ("updated", True)}


@router.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", status: str = "all", type: str = "all",
            sort: str = "title_asc", state: str = "", msg: str = ""):
    db = request.app.state.db
    today = time.strftime("%Y-%m-%d")
    if state:  # old links (/library?state=X)
        status = {"Done": "collected", "Failed": "failed", "Waiting": "upcoming"}.get(state, "missing")
    entries = []
    if type in ("all", "movie"):
        for m in db.query("SELECT id, tmdb_id, title, year, poster, state, release_date, release_name,"
                          " added_at, updated_at FROM items WHERE kind='movie'"):
            st = m["state"]
            entries.append({
                "key": f"m:{m['id']}", "kind": "movie", "href": f"/item/{m['id']}", "title": m["title"],
                "year": m["year"], "poster": m["poster"], "state": st, "badge": st,
                "collected": st == "Done", "missing": st != "Done", "failed": st == "Failed",
                "upcoming": st == "Waiting" and (m["release_date"] or "") > today,
                "added": m["added_at"], "updated": m["updated_at"], "sub": m["release_name"] or "",
            })
    if type in ("all", "show"):
        for sh in db.query(
                "SELECT s.tmdb_id, s.title, s.year, s.poster, s.added_at, COUNT(i.id) total,"
                " SUM(i.state='Done') done, SUM(i.state='Failed') failed,"
                " SUM(i.state IN ('Wanted','Searching','Downloading')) active,"
                " SUM(i.release_date > ?) future, MAX(i.updated_at) updated"
                " FROM shows s LEFT JOIN items i ON i.kind='episode' AND i.tmdb_id=s.tmdb_id"
                " GROUP BY s.tmdb_id", (today,)):
            done, total = sh["done"] or 0, sh["total"] or 0
            entries.append({
                "key": f"s:{sh['tmdb_id']}", "kind": "show", "href": f"/show/{sh['tmdb_id']}",
                "title": sh["title"], "year": sh["year"], "poster": sh["poster"],
                "state": "Done" if total and done == total else ("Downloading" if sh["active"] else "Wanted"),
                "badge": f"{done}/{total}", "collected": done > 0, "missing": done < total,
                "failed": bool(sh["failed"]), "upcoming": bool(sh["future"]),
                "added": sh["added_at"], "updated": sh["updated"] or sh["added_at"],
                "sub": f"{sh['active'] or 0} in progress" if sh["active"] else "",
            })
    if q:
        ql = q.lower()
        entries = [e for e in entries if ql in (e["title"] or "").lower()]
    if status != "all":
        entries = [e for e in entries if e.get(status)]
    field_, rev = LIB_SORTS.get(sort, LIB_SORTS["title_asc"])
    entries.sort(key=lambda e: (e[field_] is None, e[field_] or 0) if field_ != "title"
                 else (e["title"] or "").lower(), reverse=rev)
    return render(request, "library.html", entries=entries, q=q, status=status, type=type,
                  sort=sort, msg=msg)


@router.post("/library/bulk")
async def library_bulk(request: Request):
    form = await request.form()
    action = form.get("action", "delete")
    db = request.app.state.db
    n = 0
    for key in form.getlist("ids"):
        kind, _, ident = str(key).partition(":")
        if not ident.isdigit():
            continue
        if kind == "m":
            n += await (manage.delete_and_block(db, [int(ident)]) if action == "delete_block"
                        else manage.delete_items(db, [int(ident)]))
        elif kind == "s":
            await manage.delete_show(db, int(ident), block=action == "delete_block")
            n += 1
    return back(f"/library?msg={quote(f'Deleted {n} title(s)')}")


@router.get("/show/{tmdb_id}", response_class=HTMLResponse)
def show(request: Request, tmdb_id: int):
    db = request.app.state.db
    rows = db.query("SELECT * FROM shows WHERE tmdb_id=?", (tmdb_id,))
    if not rows:
        return back("/library")
    eps = db.query("SELECT id, season, episode, state, release_name, release_date, next_attempt_at"
                   " FROM items WHERE kind='episode' AND tmdb_id=? ORDER BY season, episode",
                   (tmdb_id,))
    seasons: dict[int, list] = {}
    for e in eps:
        seasons.setdefault(e["season"], []).append(e)
    cfg = config.get()
    return render(request, "show.html", show=rows[0], seasons=seasons, nav="library",
                  packs_ok=bool(cfg.debrid.provider and cfg.debrid.api_key))


@router.get("/item/{item_id}", response_class=HTMLResponse)
def item(request: Request, item_id: int):
    db = request.app.state.db
    it = db.get_item(item_id)
    if not it:
        return back("/library")
    tried = db.query("SELECT release_key, name, score, outcome, at FROM releases_tried"
                     " WHERE item_id=? ORDER BY at DESC", (item_id,))
    events = db.query("SELECT at, message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30",
                      (item_id,))
    if it.kind == "movie":
        poster = db.query("SELECT poster FROM items WHERE id=?", (item_id,))[0]["poster"]
    else:
        rows = db.query("SELECT poster FROM shows WHERE tmdb_id=?", (it.tmdb_id,))
        poster = rows[0]["poster"] if rows else None
    return render(request, "item.html", it=it, tried=tried, events=events, poster=poster,
                  blocked=blacklist.release_keys(db), manual_qs=urlencode(interactive.params_for_item(it)),
                  nav="library")


@router.post("/item/{item_id}/{action}")
async def item_action(request: Request, item_id: int, action: str):
    db = request.app.state.db
    it = db.get_item(item_id)
    if action == "retry":
        manage.retry(db, item_id)
    elif action == "replace":
        manage.replace(db, item_id)
    elif action in ("delete", "delete_block"):
        if action == "delete":
            await manage.delete_items(db, [item_id])
        else:
            await manage.delete_and_block(db, [item_id])
        if it and it.kind == "episode":
            return back(f"/show/{it.tmdb_id}")
        return back("/library")
    elif action == "block_release":
        form = await request.form()
        if form.get("key"):
            await manage.block_release(db, item_id, form["key"])
    return back(f"/item/{item_id}")


@router.post("/show/{tmdb_id}/delete")
async def show_delete(request: Request, tmdb_id: int, block: int = Form(0)):
    await manage.delete_show(request.app.state.db, tmdb_id, block=bool(block))
    return back("/library")


@router.post("/show/{tmdb_id}/season/{season}/delete")
async def season_delete(request: Request, tmdb_id: int, season: int, block: int = Form(0)):
    await manage.delete_season(request.app.state.db, tmdb_id, season, block=bool(block))
    return back(f"/show/{tmdb_id}")


@router.post("/show/{tmdb_id}/retry")
async def show_retry(request: Request, tmdb_id: int):
    db = request.app.state.db
    for r in db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=? AND state IN"
                      " ('Failed','Waiting')", (tmdb_id,)):
        manage.retry(db, r["id"])
    return back(f"/show/{tmdb_id}")


# ------------------------------------------------------------------ add


@router.get("/add")
def add_redirect(q: str = ""):
    return back(f"/discover?q={quote(q)}" if q else "/discover")


DISCOVER_ROWS = [("trending_movies", "Trending Movies"), ("trending_shows", "Trending Shows"),
                 ("popular_movies", "Popular Movies"), ("popular_shows", "Popular Shows"),
                 ("upcoming_movies", "Upcoming Movies"), ("on_the_air", "On The Air")]


def _mark_library(db, cards: list[dict]) -> None:
    have_movies = {r["tmdb_id"]: r["state"] for r in db.query("SELECT tmdb_id, state FROM items WHERE kind='movie'")}
    have_shows = {r["tmdb_id"] for r in db.query("SELECT tmdb_id FROM shows")}
    blocked = {(r["kind"], r["tmdb_id"]) for r in db.query("SELECT kind, tmdb_id FROM blacklist WHERE season=-1")}
    for c in cards:
        if c["kind"] == "movie":
            c["state"] = have_movies.get(c["tmdb_id"])
        else:
            c["state"] = "In library" if c["tmdb_id"] in have_shows else None
        c["blocked"] = (c["kind"], c["tmdb_id"]) in blocked


@router.get("/discover", response_class=HTMLResponse)
async def discover(request: Request, q: str = "", tab: str = "", error: str = ""):
    db = request.app.state.db
    rows, results = [], []
    try:
        if q:
            results = await tmdb.search(q)
            _mark_library(db, results)
        else:
            wanted = [r for r in DISCOVER_ROWS if not tab or r[0] == tab]
            got = await asyncio.gather(*(tmdb.browse(db, key) for key, _ in wanted), return_exceptions=True)
            for (key, label), cards in zip(wanted, got):
                if isinstance(cards, Exception):
                    continue
                _mark_library(db, cards)
                rows.append({"key": key, "label": label, "cards": cards if tab else cards[:14]})
    except Exception as e:
        error = error or str(e)
    return render(request, "discover.html", q=q, tab=tab, rows=rows, results=results, error=error,
                  tabs=DISCOVER_ROWS, nav="discover")


@router.get("/discover/{kind}/{tmdb_id}", response_class=HTMLResponse)
async def discover_details(request: Request, kind: str, tmdb_id: int, msg: str = "", error: str = ""):
    db = request.app.state.db
    kind = "movie" if kind == "movie" else "show"
    d = await tmdb.details(db, kind, tmdb_id)
    _mark_library(db, [d])
    episodes, states, item, season_done = {}, {}, None, {}
    if kind == "show":
        info = await tmdb.show(db, tmdb_id)
        episodes = {int(k): v for k, v in (info.get("seasons") or {}).items()}
        for r in db.query("SELECT id, season, episode, state FROM items WHERE kind='episode' AND tmdb_id=?", (tmdb_id,)):
            states[(r["season"], r["episode"])] = (r["id"], r["state"])
            if r["state"] == "Done":
                season_done[r["season"]] = season_done.get(r["season"], 0) + 1
    else:
        rows = db.query("SELECT id FROM items WHERE kind='movie' AND tmdb_id=?", (tmdb_id,))
        item = rows[0]["id"] if rows else None
    blocked_seasons = {r["season"] for r in db.query(
        "SELECT season FROM blacklist WHERE kind='show' AND tmdb_id=?", (tmdb_id,))}
    cfg = config.get()
    return render(request, "discover_details.html", d=d, kind=kind, episodes=episodes, states=states,
                  item=item, msg=msg, error=error, blocked_seasons=blocked_seasons,
                  season_done=season_done,
                  packs_ok=bool(cfg.debrid.provider and cfg.debrid.api_key),
                  today=time.strftime("%Y-%m-%d"), nav="discover")


# ------------------------------------------------------------------ manual release picker


@router.get("/releases", response_class=HTMLResponse)
def releases_page(request: Request, kind: str, tmdb_id: int, season: int | None = None,
                  episode: int | None = None, back_to: str = ""):
    params = {"kind": kind, "tmdb_id": tmdb_id}
    if season is not None:
        params["season"] = season
    if episode is not None:
        params["episode"] = episode
    return render(request, "releases.html", params=params, qs=urlencode(params),
                  back_to=back_to if back_to.startswith("/") else "", nav="discover")


@router.get("/api/releases")
async def api_releases(request: Request, kind: str, tmdb_id: int, season: int | None = None,
                       episode: int | None = None):
    if kind not in ("movie", "episode", "season") or (kind != "movie" and season is None) \
            or (kind == "episode" and episode is None):
        return JSONResponse({"error": "bad request"}, status_code=400)
    db = request.app.state.db
    try:
        s = await interactive.run(db, kind, tmdb_id, season, episode)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return interactive.to_json(db, s)


@router.post("/api/releases/grab")
async def api_grab(request: Request):
    body = await request.json()
    ok, msg, item_id = await interactive.grab(request.app.state.db, str(body.get("id", "")),
                                              int(body.get("i", -1)))
    return {"ok": ok, "message": msg, "item": item_id}


@router.post("/add")
async def add_submit(request: Request, kind: str = Form(...), tmdb_id: int = Form(...),
                     q: str = Form(""), seasons: str = Form(""), back_to: str = Form("")):
    db = request.app.state.db
    dest = back_to if back_to.startswith("/") else (f"/discover?q={quote(q)}" if q else "/library")
    sep = "&" if "?" in dest else "?"
    try:
        if kind == "movie":
            await catalog.add_movie(db, tmdb_id)
        else:
            wanted = [int(s) for s in seasons.replace(" ", "").split(",") if s.isdigit()] or None
            await catalog.add_show(db, tmdb_id, seasons=wanted)
    except catalog.Blacklisted as e:
        return back(f"{dest}{sep}error={quote(str(e) + ' (remove it on the Blacklist page)')}")
    return back(f"{dest}{sep}msg={quote('Added; it will be searched automatically')}"
                if back_to else dest)


@router.post("/add/block")
async def add_block(request: Request, kind: str = Form(...), tmdb_id: int = Form(...),
                    title: str = Form(""), q: str = Form(""), back_to: str = Form("")):
    blacklist.block(request.app.state.db, "movie" if kind == "movie" else "show", tmdb_id, title,
                    reason="blocked from Discover")
    return back(back_to if back_to.startswith("/") else f"/discover?q={quote(q)}")


# ------------------------------------------------------------------ database

DB_SORTS = {"title": "title, season, episode", "state": "state, title", "added": "added_at DESC",
            "release": "release_date DESC", "done": "done_at DESC", "updated": "updated_at DESC"}
PAGE_SIZES = (50, 100, 250, 500)
DB_COLUMNS = [("id", "ID"), ("kind", "Type"), ("title", "Title"), ("year", "Year"), ("season", "S"),
              ("episode", "E"), ("state", "State"), ("release_date", "Release date"),
              ("release_name", "Release"), ("file_path", "File"), ("source", "Source"),
              ("attempts", "Searches"), ("imdb_id", "IMDb"), ("tmdb_id", "TMDB"), ("tvdb_id", "TVDB"),
              ("engine_id", "Engine id"), ("added_at", "Added"), ("updated_at", "Updated"),
              ("done_at", "Collected")]
DEFAULT_DB_COLUMNS = ["title", "state", "release_date", "release_name", "source", "added_at"]


@router.get("/database", response_class=HTMLResponse)
def database(request: Request, kind: str = "", state: str = "", q: str = "", source: str = "",
             sort: str = "updated", page: int = 1, msg: str = "", per: int = 100):
    db = request.app.state.db
    where, params = [], []
    if kind in ("movie", "episode"):
        where.append("kind=?")
        params.append(kind)
    if state:
        where.append("state=?")
        params.append(state)
    if q:
        where.append("(title LIKE ? OR release_name LIKE ? OR imdb_id=? OR CAST(tmdb_id AS TEXT)=?)")
        params += [f"%{q}%", f"%{q}%", q, q]
    if source:
        where.append("COALESCE(source, 'manual')=?")
        params.append(source)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.query(f"SELECT COUNT(*) n FROM items {clause}", tuple(params))[0]["n"]
    page = max(1, page)
    per = per if per in PAGE_SIZES else 100
    rows = db.query(
        "SELECT id, kind, tmdb_id, tvdb_id, imdb_id, title, year, season, episode, state, release_date,"
        " release_name, file_path, engine_id, attempts, added_at, updated_at, done_at, source, mode"
        f" FROM items {clause} ORDER BY {DB_SORTS.get(sort, DB_SORTS['updated'])} LIMIT ? OFFSET ?",
        (*params, per, (page - 1) * per))
    sources = [r["s"] for r in db.query("SELECT DISTINCT COALESCE(source, 'manual') s FROM items ORDER BY s")]
    args = {"kind": kind, "state": state, "q": q, "source": source, "sort": sort, "per": per}
    return render(request, "database.html", rows=rows, total=total, page=page,
                  pages=max(1, -(-total // per)), args=args, states=[s.value for s in State],
                  columns=DB_COLUMNS, default_columns=DEFAULT_DB_COLUMNS, page_sizes=PAGE_SIZES,
                  sources=sources, sorts=list(DB_SORTS), msg=msg,
                  qs=urlencode({k: v for k, v in args.items() if v and not (k == "per" and v == 100)}))


@router.post("/database/bulk")
async def database_bulk(request: Request):
    form = await request.form()
    action = form.get("action", "")
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    back_to = form.get("back", "/database")
    if not back_to.startswith("/"):
        back_to = "/database"
    if action not in manage.BULK_ACTIONS or not ids:
        return back(back_to)
    n = await manage.bulk(request.app.state.db, action, ids)
    sep = "&" if "?" in back_to else "?"
    return back(f"{back_to}{sep}msg={quote(f'{action.replace(chr(95), chr(32))}: {n} item(s)')}")


@router.post("/database/row/{item_id}/{action}")
async def database_row(request: Request, item_id: int, action: str):
    form = await request.form()
    back_to = form.get("back", "/database")
    if not back_to.startswith("/"):
        back_to = "/database"
    if action in ("rescrape", "delete"):
        n = await manage.bulk(request.app.state.db, action, [item_id])
        sep = "&" if "?" in back_to else "?"
        back_to = f"{back_to}{sep}msg={quote(f'{action}: {n} item(s)')}"
    return back(back_to)


# ------------------------------------------------------------------ blacklist


@router.get("/blacklist", response_class=HTMLResponse)
def blacklist_page(request: Request):
    db = request.app.state.db
    return render(request, "blacklist.html", titles=blacklist.titles(db),
                  releases=blacklist.releases(db))


@router.post("/blacklist/remove")
async def blacklist_remove(request: Request, kind: str = Form(...), tmdb_id: int = Form(...),
                           season: int = Form(-1)):
    blacklist.unblock(request.app.state.db, kind, tmdb_id, None if season < 0 else season)
    return back("/blacklist")


@router.post("/blacklist/release/remove")
async def blacklist_release_remove(request: Request, key: str = Form(...)):
    blacklist.unblock_release(request.app.state.db, key)
    return back("/blacklist")


# ------------------------------------------------------------------ calendar


@router.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, days: int = 30):
    db = request.app.state.db
    days = min(max(days, 7), 120)
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
    rows = db.query(
        "SELECT i.id, i.kind, i.tmdb_id, i.title, i.year, i.season, i.episode, i.release_date,"
        " i.state, COALESCE(i.poster, s.poster) poster FROM items i"
        " LEFT JOIN shows s ON i.kind='episode' AND s.tmdb_id=i.tmdb_id"
        " WHERE i.release_date BETWEEN ? AND ? ORDER BY i.release_date, i.title, i.season, i.episode",
        (start, end))
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["release_date"][:10], []).append(r)
    return render(request, "calendar.html", by_day=by_day, today=time.strftime("%Y-%m-%d"),
                  days=days)


# ------------------------------------------------------------------ settings


def _rows(form, prefix: str, fields: list[str]) -> list[dict]:
    """Collect repeated form rows named prefix-<n>-<field>; blank rows are dropped."""
    rows: dict[int, dict] = {}
    for key, value in form.multi_items():
        parts = key.split("-", 2)
        if len(parts) != 3 or parts[0] != prefix or not parts[1].isdigit():
            continue
        _, idx, field = parts
        if field in fields:
            rows.setdefault(int(idx), {})[field] = value
    return [rows[i] for i in sorted(rows)]


def config_from_form(form, base: Config) -> Config:
    d = base.model_dump()
    d["plex"] = {"url": form.get("plex.url", "").strip(), "token": form.get("plex.token", "").strip()}
    d["debrid"] = {"provider": form.get("debrid.provider", ""),
                   "api_key": form.get("debrid.api_key", "").strip()}
    for k in ("tmdb_api_key", "tvdb_api_key", "trakt_client_id", "mdblist_api_key", "web_password"):
        if k in form:
            d[k] = form.get(k, "").strip()
    d["mount"] = {"path": form.get("mount.path", d["mount"]["path"]).strip(),
                  "cache_size_gb": int(form.get("mount.cache_size_gb") or d["mount"]["cache_size_gb"])}
    d["library"] = {"mode": form.get("library.mode", d["library"]["mode"]),
                    "movies_path": form.get("library.movies_path", d["library"]["movies_path"]).strip(),
                    "shows_path": form.get("library.shows_path", d["library"]["shows_path"]).strip()}
    q = d["quality"]
    d["quality"] = {
        "preset": form.get("quality.preset", q["preset"]),
        "min_size_gb": float(form.get("quality.min_size_gb") or 0),
        "max_size_gb": float(form.get("quality.max_size_gb") or 0),
        "language": form.get("quality.language", q["language"]).strip(),
        "must_contain": [w.strip() for w in form.get("quality.must_contain", "").split(",") if w.strip()],
        "never_contain": [w.strip() for w in form.get("quality.never_contain", "").split(",") if w.strip()],
    }
    if "upgrade_days" in form:
        d["upgrade_days"] = int(form.get("upgrade_days") or 0)
    if "usenet-present" in form:
        d["usenet"] = [
            {"host": r["host"].strip(), "port": int(r.get("port") or 563), "ssl": r.get("ssl") == "on",
             "username": r.get("username", "").strip(), "password": r.get("password", ""),
             "connections": int(r.get("connections") or 10)}
            for r in _rows(form, "usenet", ["host", "port", "ssl", "username", "password", "connections"])
            if r.get("host", "").strip()]
    if "indexers-present" in form:
        d["indexers"] = [
            {"type": r["type"], "url": r.get("url", "").strip(), "api_key": r.get("api_key", "").strip(),
             "enabled": r.get("enabled") == "on"}
            for r in _rows(form, "indexers", ["type", "url", "api_key", "enabled"])
            if r.get("type") and (r.get("url", "").strip() or r["type"] == "torrentio")]
    if "notify-present" in form:
        d["notifications"] = [
            {"type": r["type"], "url": r.get("url", "").strip(), "token": r.get("token", "").strip(),
             "chat_id": r.get("chat_id", "").strip(), "enabled": r.get("enabled") == "on"}
            for r in _rows(form, "notify", ["type", "url", "token", "chat_id", "enabled"])
            if r.get("type") and (r.get("url", "").strip() or r.get("token", "").strip())]
        d["notify_on"] = {k: form.get(f"notify_on.{k}") == "on"
                          for k in ("collected", "upgraded", "failed", "blacklisted")}
    if "sources-present" in form:
        d["sources"] = [
            {"type": r["type"], "value": r.get("value", "").strip(), "enabled": r.get("enabled") == "on"}
            for r in _rows(form, "sources", ["type", "value", "enabled"])
            if r.get("type") and (r.get("value", "").strip() or r["type"] == "plex_watchlist")]
    return Config.model_validate(d)


ENGINE_KEYS = ("debrid", "usenet", "mount")


async def _apply(request: Request, new: Config) -> None:
    old = config.get()
    config.save(new)
    if any(getattr(old, k) != getattr(new, k) for k in ENGINE_KEYS):
        await request.app.state.engine.restart()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    return render(request, "settings.html", c=config.get(), saved=saved, error="",
                  debrids=DEBRIDS, presets=PRESETS, indexer_types=INDEXER_TYPES,
                  source_types=SOURCE_TYPES)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    try:
        new = config_from_form(form, config.get())
    except (ValidationError, ValueError) as e:
        return render(request, "settings.html", c=config.get(), saved=0, error=str(e),
                      debrids=DEBRIDS, presets=PRESETS, indexer_types=INDEXER_TYPES,
                      source_types=SOURCE_TYPES)
    await _apply(request, new)
    return back("/settings?saved=1")


@router.post("/api/test")
async def test_connection(request: Request):
    """Test one thing from the current form (used by the Test buttons)."""
    form = await request.form()
    what = form.get("what", "")
    try:
        cfg = config_from_form(form, config.get())
    except (ValidationError, ValueError) as e:
        return JSONResponse({"ok": False, "message": str(e)})
    from ..library import plex
    if what == "tmdb":
        msg = await checks.tmdb(cfg.tmdb_api_key)
    elif what == "tvdb":
        msg = await checks.tvdb(cfg.tvdb_api_key)
    elif what == "debrid":
        msg = await checks.debrid(cfg.debrid.provider, cfg.debrid.api_key)
    elif what == "plex":
        msg = await plex.test_connection(cfg.plex.url, cfg.plex.token) if cfg.plex.url else "missing URL"
    elif what.startswith("usenet-"):
        i = int(what.split("-")[1])
        msg = await checks.usenet(cfg.usenet[i]) if i < len(cfg.usenet) else "fill in the host first"
    elif what.startswith("notify-"):
        i = int(what.split("-")[1])
        msg = await notify.test(cfg.notifications[i]) if i < len(cfg.notifications) else "fill in the row first"
    elif what.startswith("indexers-"):
        i = int(what.split("-")[1])
        msg = await checks.indexer(cfg.indexers[i]) if i < len(cfg.indexers) else "fill in the row first"
    else:
        msg = "unknown test"
    return JSONResponse({"ok": not msg, "message": msg or "Connected"})


# ------------------------------------------------------------------ setup wizard

SETUP_STEPS = ["metadata", "downloads", "indexers", "library"]


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, step: str = "metadata"):
    if step not in SETUP_STEPS:
        step = "metadata"
    return render(request, "setup.html", c=config.get(), step=step, steps=SETUP_STEPS,
                  debrids=DEBRIDS, presets=PRESETS, indexer_types=INDEXER_TYPES, error="",
                  source_types=SOURCE_TYPES, nav="setup")


@router.post("/setup", response_class=HTMLResponse)
async def setup_save(request: Request):
    form = await request.form()
    step = form.get("step", "metadata")
    try:
        new = config_from_form(form, config.get())
    except (ValidationError, ValueError) as e:
        return render(request, "setup.html", c=config.get(), step=step, steps=SETUP_STEPS,
                      debrids=DEBRIDS, presets=PRESETS, indexer_types=INDEXER_TYPES, error=str(e),
                      source_types=SOURCE_TYPES, nav="setup")
    error = ""
    if step == "metadata":
        error = await checks.tmdb(new.tmdb_api_key) or (
            "TVDB: " + m if (m := await checks.tvdb(new.tvdb_api_key)) else "")
        if error.startswith("missing"):
            error = "A TMDB API key is required"
    elif step == "downloads":
        if not ((new.debrid.provider and new.debrid.api_key) or new.usenet):
            error = "Add a debrid service or a usenet provider (or both)"
        else:
            error = await checks.debrid(new.debrid.provider, new.debrid.api_key)
            for p in new.usenet:
                error = error or (f"{p.host}: " + m if (m := await checks.usenet(p)) else "")
    elif step == "indexers" and not new.indexers:
        error = "Add at least one indexer"
    await _apply(request, new)  # save even with a warning; the user may fix later
    if error:
        return render(request, "setup.html", c=config.get(), step=step, steps=SETUP_STEPS,
                      debrids=DEBRIDS, presets=PRESETS, indexer_types=INDEXER_TYPES, error=error,
                      source_types=SOURCE_TYPES, nav="setup")
    i = SETUP_STEPS.index(step)
    if i + 1 < len(SETUP_STEPS):
        return back(f"/setup?step={SETUP_STEPS[i + 1]}")
    return back("/")


# ------------------------------------------------------------------ logs


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request, lines: int = 300):
    path = config.DATA_DIR / "logs" / "clid2.log"
    text = ""
    if path.exists():
        with path.open(errors="replace") as f:
            text = "".join(deque(f, maxlen=min(max(lines, 50), 5000)))
    return render(request, "logs.html", text=text, lines=lines)


@router.get("/api/status")
async def api_status(request: Request):
    db = request.app.state.db
    return {"counts": db.state_counts(), "engine": await engine_summary(request)}


def _json(obj) -> str:
    return json.dumps(obj)


templates.env.filters["tojson_attr"] = _json
