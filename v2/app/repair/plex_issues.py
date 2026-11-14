"""Plex "Report issue" -> automatic replacement.

Plex sends reports to the server owner through an undocumented GraphQL API
(community.plex.tv/api). Reports can't be resolved there, so handled ids are
stored locally. On the very first run every existing report is marked seen,
so old reports don't trigger replacements.
"""
from __future__ import annotations

import logging
import re
import time

from .. import config, http
from ..db import DB
from ..models import State

log = logging.getLogger("clid2.plex_issues")

COMMUNITY = "https://community.plex.tv/api"
REPORT_URL = re.compile(
    r"^server://(?P<server>[0-9a-fA-F]+)/com\.plexapp\.plugins\.library/library/metadata/(?P<key>\d+)")
QUERY = """query getReportedIssues($first: PaginationInt!, $after: String) {
  reports(after: $after, first: $first) {
    nodes { id message user { username displayName } url date }
    pageInfo { hasNextPage endCursor } } }"""
REPLY = """mutation createReportComment($input: CreateReportCommentInput!) {
  createReportComment(input: $input) { id } }"""


def _headers(token: str) -> dict:
    return {"X-Plex-Token": token, "Accept": "application/json",
            "Content-Type": "application/json", "X-Plex-Product": "cli_debrid",
            "X-Plex-Client-Identifier": "cli_debrid-2"}


async def _graphql(token: str, query: str, variables: dict) -> dict:
    resp = await http.request("POST", COMMUNITY, headers=_headers(token),
                              json={"query": query, "variables": variables})
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Plex community API: {body['errors']}")
    return body.get("data") or {}


async def fetch_reports(token: str) -> list[dict]:
    d = await _graphql(token, QUERY, {"first": 50})
    return (d.get("reports") or {}).get("nodes") or []


async def reply(token: str, report_id: str, message: str) -> None:
    try:
        await _graphql(token, REPLY, {"input": {"message": message, "report": report_id}})
    except Exception as e:
        log.warning("could not reply to Plex report %s: %s", report_id, e)


def _ids(md: dict) -> dict:
    out = {}
    for g in md.get("Guid") or []:
        scheme, _, val = (g.get("id") or "").partition("://")
        if val:
            out[scheme] = val
    return out


async def _metadata(key: str) -> dict | None:
    c = config.get().plex
    d = await http.get_json(f"{c.url.rstrip('/')}/library/metadata/{key}",
                            params={"includeGuids": 1},
                            headers={"X-Plex-Token": c.token, "Accept": "application/json"})
    mds = d.get("MediaContainer", {}).get("Metadata") or []
    return mds[0] if mds else None


async def _machine_id() -> str:
    c = config.get().plex
    d = await http.get_json(f"{c.url.rstrip('/')}/identity",
                            headers={"X-Plex-Token": c.token, "Accept": "application/json"})
    return d.get("MediaContainer", {}).get("machineIdentifier", "")


def _save(db: DB, report: dict, status: str, item_id: int | None = None, title: str = "") -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO plex_reports(id, item_id, title, status, message, at)"
                  " VALUES (?,?,?,?,?,?)",
                  (report["id"], item_id, title, status, report.get("message", ""), time.time()))


async def find_item(db: DB, md: dict) -> int | None:
    if md.get("type") == "movie":
        tmdb = _ids(md).get("tmdb")
        rows = db.query("SELECT id FROM items WHERE kind='movie' AND tmdb_id=?", (tmdb,)) if tmdb else []
    elif md.get("type") == "episode":
        show = await _metadata(md["grandparentRatingKey"]) if md.get("grandparentRatingKey") else None
        tmdb = _ids(show or {}).get("tmdb")
        rows = db.query("SELECT id FROM items WHERE kind='episode' AND tmdb_id=? AND season=? AND"
                        " episode=?", (tmdb, md.get("parentIndex"), md.get("index"))) if tmdb else []
    else:
        return None
    return rows[0]["id"] if rows else None


def start_replace(db: DB, item_id: int, reason: str) -> bool:
    """Send a Done item back to search, excluding its current release."""
    it = db.get_item(item_id)
    if not it or it.state is not State.DONE:
        return False
    rows = db.query("SELECT release_key, name FROM releases_tried WHERE item_id=? AND"
                    " outcome='picked'", (item_id,))
    for r in rows:
        db.record_release(item_id, r["release_key"], r["name"], None, "replaced")
    db.set_state(item_id, State.WANTED, mode="replace", attempts=0, next_attempt_at=None)
    db.log_event(f"Replacing {it.label}: {reason}", item_id)
    return True


async def process(db: DB) -> None:
    c = config.get().plex
    if not (c.url and c.token):
        return
    reports = await fetch_reports(c.token)
    known = {r["id"] for r in db.query("SELECT id FROM plex_reports")}
    first_run = not db.query("SELECT 1 FROM kv WHERE key='plex_reports_initialized'")
    if first_run:
        for rep in reports:
            _save(db, rep, "seen")
        with db.tx() as tx:
            tx.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('plex_reports_initialized', '1')")
        return
    new = [r for r in reports if r["id"] not in known]
    if not new:
        return
    machine = await _machine_id()
    for rep in new:
        m = REPORT_URL.match(rep.get("url") or "")
        if not m or m.group("server").lower() != machine.lower():
            _save(db, rep, "ignored")
            continue
        md = await _metadata(m.group("key"))
        if not md:
            _save(db, rep, "no_match")
            continue
        item_id = await find_item(db, md)
        who = (rep.get("user") or {}).get("displayName") or "someone"
        if item_id and start_replace(db, item_id, f"reported by {who}: {rep.get('message', '')}"):
            _save(db, rep, "replacing", item_id, md.get("title", ""))
            await reply(c.token, rep["id"], "Thanks! A new copy is being fetched; it should be "
                                            "back shortly.")
        else:
            _save(db, rep, "no_match", item_id, md.get("title", ""))
