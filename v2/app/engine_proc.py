"""The bundled mount engine (a Go binary): config, start/stop, status.

Two ways it can be run:

* In the container, run.sh owns the engine process (CLID2_ENGINE_MANAGED=external)
  and the app only writes its config and signals it. Restarting the app
  (live patching) therefore never restarts the mount.
* Elsewhere (development), EngineProcess runs the engine as a child.

Either way the engine is restarted only when its generated config.json
actually changes, never just because settings were saved.
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
        """Apply new settings: restart only if the engine's config changed."""
        if self.running and engine_config.write(config.get(), self.config_dir()):
            log.info("engine settings changed; restarting the mount")
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


def is_fuse_mount(path: str, mountinfo: str | None = None) -> bool:
    """True if `path` is the mount point of a FUSE filesystem.

    Only FUSE mounts are ever unmounted: when running as root, unmounting a
    plain bind mount at this path would cut the mount off from the host.
    """
    if not path:
        return False
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            return False
    target = os.path.normpath(path)
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5 or "-" not in fields:
            continue
        mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        fstype = fields[fields.index("-") + 1] if fields.index("-") + 1 < len(fields) else ""
        if mp == target and fstype.startswith("fuse"):
            return True
    return False


RUN_DIR = Path(os.environ.get("CLID2_RUN_DIR", "/run/clid2"))


class ExternalEngine:
    """The engine is run by run.sh; we write its config and signal it.

    Starting or stopping the app leaves the engine (and the mount) alone.
    """

    def __init__(self, binary: str = ENGINE_BIN, run_dir: Path | None = None):
        self.binary = binary
        self.run_dir = run_dir or RUN_DIR
        self.last_exit = None

    @property
    def available(self) -> bool:
        return Path(self.binary).is_file() and os.access(self.binary, os.X_OK)

    def _pid(self) -> int | None:
        try:
            return int((self.run_dir / "engine.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    @property
    def running(self) -> bool:
        pid = self._pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @property
    def restarts(self) -> int:
        try:
            return int((self.run_dir / "engine.restarts").read_text().strip())
        except (OSError, ValueError):
            return 0

    def config_dir(self) -> Path:
        return config.DATA_DIR / "engine"

    def _signal_restart(self, why: str) -> None:
        pid = self._pid()
        if not pid:
            return
        log.info("%s; restarting the mount", why)
        (self.run_dir / "engine.restart-requested").touch()
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    def _sync(self) -> None:
        cfg = config.get()
        if not engine_config.ready(cfg):
            if engine_config.remove(self.config_dir()) and self.running:
                self._signal_restart("no debrid or usenet left")
            return
        if engine_config.write(cfg, self.config_dir()) and self.running:
            self._signal_restart("engine settings changed")

    def start(self) -> None:
        self._sync()
        if self.running:
            log.info("mount engine already running (pid %s); keeping it", self._pid())

    async def restart(self) -> None:
        self._sync()

    async def stop(self) -> None:
        """The app is stopping (maybe just for a live patch): leave the engine running."""


def make_engine():
    if os.environ.get("CLID2_ENGINE_MANAGED") == "external":
        return ExternalEngine()
    return EngineProcess()


def lazy_unmount(path: str) -> None:
    """Clear a dead FUSE mount so the next start can mount again."""
    if not is_fuse_mount(path):
        return
    for cmd in (["fusermount3", "-uz", path], ["fusermount", "-uz", path], ["umount", "-l", path]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True, timeout=10)
            if not os.path.ismount(path):
                log.info("unmounted stale mount %s", path)
                return
