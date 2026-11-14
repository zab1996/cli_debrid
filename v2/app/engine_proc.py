"""Starts, watches and stops the bundled mount engine (a Go binary).

The engine's config is generated from our settings on every start, so the
user only ever edits one config. Engine output is forwarded into our log.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config, engine_config

log = logging.getLogger("clid2.engine")
engine_log = logging.getLogger("clid2.engine.out")

ENGINE_BIN = os.environ.get("CLID2_ENGINE_BIN", "/app/bin/clid2-engine")
BACKOFF = [2, 5, 15, 30, 60]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEALTHY_AFTER = 120  # seconds of uptime that reset the restart backoff


class EngineProcess:
    def __init__(self, binary: str = ENGINE_BIN):
        self.binary = binary
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.started_at = 0.0
        self.last_exit: int | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return Path(self.binary).is_file() and os.access(self.binary, os.X_OK)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def config_dir(self) -> Path:
        return config.DATA_DIR / "engine"

    def start(self) -> None:
        if not self.available:
            log.warning("engine binary %s not found; mount disabled", self.binary)
            return
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            cfg = config.get()
            if not engine_config.ready(cfg):
                # Nothing to mount yet (no debrid or usenet configured).
                await self._sleep(30)
                continue
            engine_config.write(cfg, self.config_dir())
            self.started_at = time.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    self.binary, "--config", str(self.config_dir()),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True, limit=1024 * 1024,
                )
            except OSError as e:
                log.error("could not start engine: %s", e)
                await self._sleep(60)
                continue
            log.info("engine started (pid %s)", self.proc.pid)
            pump = asyncio.create_task(self._pump(self.proc))
            self.last_exit = await self.proc.wait()
            await pump
            if self._stop.is_set():
                break
            up = time.time() - self.started_at
            if up > HEALTHY_AFTER:
                self.restarts = 0
            delay = BACKOFF[min(self.restarts, len(BACKOFF) - 1)]
            self.restarts += 1
            log.error("engine exited with %s after %ds; restarting in %ds",
                      self.last_exit, int(up), delay)
            lazy_unmount(cfg.mount.path)
            await self._sleep(delay)

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # a single line over the limit; skip it
                continue
            if not line:
                return
            text = _ANSI.sub("", line.decode(errors="replace")).rstrip()
            if text:
                engine_log.info(text)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def restart(self) -> None:
        """Apply new settings: the supervisor rewrites config and starts again."""
        if self.running:
            self.proc.send_signal(signal.SIGTERM)

    async def stop(self) -> None:
        self._stop.set()
        if self.running:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=20)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._task:
            await self._task
        lazy_unmount(config.get().mount.path)


def lazy_unmount(path: str) -> None:
    """Clear a dead FUSE mount so the next start can mount again."""
    if not path or not os.path.ismount(path):
        return
    for cmd in (["fusermount3", "-uz", path], ["fusermount", "-uz", path], ["umount", "-l", path]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True, timeout=10)
            if not os.path.ismount(path):
                log.info("unmounted stale mount %s", path)
                return
