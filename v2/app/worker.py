"""The one background loop.

Every step has a fixed cadence chosen here, in code. There are no
user-facing task intervals. A step that raises is logged and retried on
its next cadence; it never stops the loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("clid2.worker")

TICK_SECONDS = 10

StepFn = Callable[[], Awaitable[None]]


@dataclass
class Step:
    name: str
    every: float           # seconds between runs
    fn: StepFn
    last_run: float = 0.0
    last_error: str | None = None
    runs: int = field(default=0)

    def due(self, now: float) -> bool:
        return now - self.last_run >= self.every


REMEMBER_AFTER = 3600   # steps that run hourly or less often keep their last run across restarts


class Worker:
    def __init__(self, tick: float = TICK_SECONDS, db=None):
        self.tick = tick
        self.steps: list[Step] = []
        self._stop = asyncio.Event()
        self.db = db

    def _load_last_runs(self) -> None:
        if self.db is None:
            return
        for s in self.steps:
            if s.every >= REMEMBER_AFTER:
                rows = self.db.query("SELECT value FROM kv WHERE key=?", (f"step:{s.name}",))
                if rows:
                    s.last_run = float(rows[0]["value"])

    def _save_last_run(self, step: "Step") -> None:
        if self.db is None or step.every < REMEMBER_AFTER:
            return
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                      (f"step:{step.name}", str(step.last_run)))

    def every(self, seconds: float, name: str | None = None):
        """Decorator: register an async step that runs every `seconds`."""
        def deco(fn: StepFn) -> StepFn:
            self.steps.append(Step(name or fn.__name__, seconds, fn))
            return fn
        return deco

    async def run_once(self, now: float | None = None) -> None:
        """Run every step that is due. Steps run one after another, in order."""
        for step in self.steps:
            t = time.time() if now is None else now
            if not step.due(t):
                continue
            step.last_run = t
            self._save_last_run(step)
            try:
                await step.fn()
                step.last_error = None
            except Exception as e:  # keep the loop alive no matter what
                step.last_error = f"{type(e).__name__}: {e}"
                log.exception("step %s failed", step.name)
            step.runs += 1

    async def run_forever(self) -> None:
        self._load_last_runs()
        log.info("worker started with steps: %s", ", ".join(s.name for s in self.steps))
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass
        log.info("worker stopped")

    def stop(self) -> None:
        self._stop.set()
