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


class Worker:
    def __init__(self, tick: float = TICK_SECONDS):
        self.tick = tick
        self.steps: list[Step] = []
        self._stop = asyncio.Event()

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
            try:
                await step.fn()
                step.last_error = None
            except Exception as e:  # keep the loop alive no matter what
                step.last_error = f"{type(e).__name__}: {e}"
                log.exception("step %s failed", step.name)
            step.runs += 1

    async def run_forever(self) -> None:
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
