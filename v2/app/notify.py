"""Notifications: Discord, Telegram, ntfy, Pushover or a plain JSON webhook.

Events are queued in the database and sent in batches by the worker, so a
season pack finishing produces one message listing its episodes rather than
a burst of pings, and nothing is lost across a restart.
"""
from __future__ import annotations

import logging
import time

from . import config, http
from .config import NotifyTarget
from .db import DB

log = logging.getLogger("clid2.notify")

EVENTS = {
    # event: (heading, emoji, discord colour)
    "collected": ("Collected", "✅", 0x22C55E),
    "upgraded": ("Replaced / upgraded", "⬆️", 0x3B82F6),
    "failed": ("Gave up (Failed)", "⚠️", 0xF59E0B),
    "blacklisted": ("Blacklisted", "⛔", 0xEF4444),
}
MAX_LINES = 25
MAX_TRIES = 5


def emit(db: DB, event: str, title: str, detail: str = "", item_id: int | None = None) -> None:
    """Queue a notification (no-op if that event is switched off or nothing is set up)."""
    cfg = config.get()
    if not any(t.enabled for t in cfg.notifications) or not getattr(cfg.notify_on, event, False):
        return
    with db.tx() as c:
        c.execute("INSERT INTO notifications(event, title, detail, item_id, at) VALUES (?,?,?,?,?)",
                  (event, title, detail, item_id, time.time()))


def _message(event: str, rows: list) -> tuple[str, list[str]]:
    heading, emoji, _ = EVENTS[event]
    lines = []
    for r in rows[:MAX_LINES]:
        lines.append(f"{r['title']}" + (f" — {r['detail']}" if r["detail"] else ""))
    if len(rows) > MAX_LINES:
        lines.append(f"…and {len(rows) - MAX_LINES} more")
    count = f" ({len(rows)})" if len(rows) > 1 else ""
    return f"{emoji} {heading}{count}", lines


async def send(t: NotifyTarget, event: str, heading: str, lines: list[str]) -> None:
    """Deliver one message to one target. Raises on failure."""
    body = "\n".join(lines)
    if t.type == "discord":
        colour = EVENTS.get(event, ("", "", 0xE8601C))[2]
        payload = {"username": "cli_debrid", "embeds": [{
            "title": heading, "description": "\n".join(f"• {x}" for x in lines)[:4000], "color": colour}]}
        resp = await http.request("POST", t.url, json=payload, retries=1)
    elif t.type == "telegram":
        text = f"<b>{_esc(heading)}</b>\n" + "\n".join(f"• {_esc(x)}" for x in lines)
        resp = await http.request("POST", f"https://api.telegram.org/bot{t.token}/sendMessage",
                                  json={"chat_id": t.chat_id, "text": text[:4000], "parse_mode": "HTML",
                                        "disable_web_page_preview": True}, retries=1)
    elif t.type == "ntfy":
        headers = {"Title": heading.encode("ascii", "ignore").decode().strip() or "cli_debrid",
                   "Tags": event}
        if t.token:
            headers["Authorization"] = f"Bearer {t.token}"
        resp = await http.request("POST", t.url, content=body.encode(), headers=headers, retries=1)
    elif t.type == "pushover":
        resp = await http.request("POST", "https://api.pushover.net/1/messages.json",
                                  data={"token": t.token, "user": t.chat_id, "title": heading,
                                        "message": body[:1000] or heading}, retries=1)
    else:  # webhook
        resp = await http.request("POST", t.url, json={"event": event, "title": heading, "items": lines},
                                  retries=1)
    if resp.status_code >= 400:
        raise http.HTTPError(resp.status_code, t.type, resp.text)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def flush(db: DB) -> None:
    """Send everything queued, one message per event type per target."""
    rows = db.query("SELECT * FROM notifications ORDER BY id LIMIT 500")
    if not rows:
        return
    targets = [t for t in config.get().notifications if t.enabled]
    if not targets:
        with db.tx() as c:
            c.execute("DELETE FROM notifications")
        return
    by_event: dict[str, list] = {}
    for r in rows:
        by_event.setdefault(r["event"], []).append(r)
    for event, items in by_event.items():
        heading, lines = _message(event, items)
        ok = True
        for t in targets:
            try:
                await send(t, event, heading, lines)
            except Exception as e:
                ok = False
                log.warning("notification to %s failed: %s", t.type, e)
        ids = [r["id"] for r in items]
        marks = ",".join("?" * len(ids))
        with db.tx() as c:
            if ok or min(r["tries"] for r in items) + 1 >= MAX_TRIES:
                c.execute(f"DELETE FROM notifications WHERE id IN ({marks})", ids)
            else:
                c.execute(f"UPDATE notifications SET tries = tries + 1 WHERE id IN ({marks})", ids)
        if ok:
            log.info("notified %s: %d %s", ",".join(t.type for t in targets), len(items), event)


async def test(t: NotifyTarget) -> str:
    """'' if a test message went through, else the problem."""
    try:
        await send(t, "collected", "✅ cli_debrid test", ["Notifications are working."])
        return ""
    except Exception as e:
        return str(e)[:200]
