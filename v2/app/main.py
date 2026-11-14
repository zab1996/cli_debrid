"""Entry point: `uvicorn app.main:app` (run from the v2/ folder)."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .db import DB
from .pipeline import register
from .web.routes import STATIC_DIR, router
from .worker import Worker

logging.basicConfig(
    level=os.environ.get("CLID2_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("clid2")


def create_app(db_path: str | None = None, start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.load()
        db = DB(db_path or str(config.DATA_DIR / "clid2.db"))
        worker = Worker()
        register(worker, db)
        app.state.db = db
        app.state.worker = worker
        task = asyncio.create_task(worker.run_forever()) if start_worker else None
        log.info("cli_debrid %s started", __version__)
        try:
            yield
        finally:
            worker.stop()
            if task:
                await task
            db.close()

    app = FastAPI(title="cli_debrid 2", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()
