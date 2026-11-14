"""Entry point: `uvicorn app.main:app` (run from the v2/ folder)."""
from __future__ import annotations

import asyncio
import base64
import logging
import logging.handlers
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import __version__, acquire, config, http
from .db import DB
from .engine_proc import make_engine
from .pipeline import register
from .web.routes import STATIC_DIR, router
from .worker import Worker

logging.basicConfig(
    level=os.environ.get("CLID2_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("clid2")
# Quiet the per-request access log; our own events are what matter.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


def _file_logging() -> None:
    d = config.DATA_DIR / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    h = logging.handlers.RotatingFileHandler(d / "clid2.log", maxBytes=10 * 1024 * 1024,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(x, logging.handlers.RotatingFileHandler) for x in root.handlers):
        root.addHandler(h)


def _authorized(request: Request) -> bool:
    password = config.get().web_password
    if not password or request.url.path == "/health":
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(given.encode(), password.encode())


def create_app(db_path: str | None = None, start_worker: bool = True,
               start_engine: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.load()
        _file_logging()
        db = DB(db_path or str(config.DATA_DIR / "clid2.db"))
        acquire.recover_interrupted(db)
        worker = Worker(db=db)
        register(worker, db)
        engine = make_engine()
        app.state.db = db
        app.state.worker = worker
        app.state.engine = engine
        app.state.started_at = time.time()
        if start_engine:
            engine.start()
        task = asyncio.create_task(worker.run_forever()) if start_worker else None
        log.info("cli_debrid %s started", __version__)
        try:
            yield
        finally:
            worker.stop()
            if task:
                await task
            await engine.stop()
            await http.close()
            db.close()

    app = FastAPI(title="cli_debrid 2", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not _authorized(request):
            return Response("Login required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="cli_debrid"'})
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()
