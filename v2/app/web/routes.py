from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..models import State

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__
templates.env.filters["ago"] = lambda ts: _ago(ts)

router = APIRouter()


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"


@router.get("/health")
def health(request: Request) -> dict:
    worker = request.app.state.worker
    return {
        "ok": True,
        "version": __version__,
        "steps": [{"name": s.name, "runs": s.runs, "error": s.last_error} for s in worker.steps],
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    db = request.app.state.db
    return templates.TemplateResponse(request, "home.html", {
        "counts": db.state_counts(),
        "states": [s.value for s in State],
        "events": db.recent_events(30),
    })
