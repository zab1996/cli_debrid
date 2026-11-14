"""Client for the bundled engine's internal API (localhost only).

API (served by v2/engine/pkg/engineapi):
  GET    /status                 -> {"ready", "mount_path", "mounted", "providers", "usenet"}
  GET    /cache?hashes=a,b       -> {"a": true, "b": false}
  POST   /add                    {"magnet"} | {"nzb_b64", "name"} -> {"id", "protocol"}
  GET    /entries/{id}           -> Entry (see EntryStatus)
  DELETE /entries/{id}
  POST   /entries/{id}/health    -> {"status"}   start a health check
  GET    /entries/{id}/health    -> {"status"}   "healthy" | "broken" | "checking" | "unknown"
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

from .. import http

ENGINE_URL = os.environ.get("CLID2_ENGINE_URL", "http://127.0.0.1:8283")


class EngineError(Exception):
    pass


class EngineDown(EngineError):
    """The engine could not be reached at all (not a verdict on the release)."""


@dataclass
class EngineFile:
    name: str          # path inside the entry, e.g. "Season 1/Show.S01E02.mkv"
    size: int
    path: str          # absolute path under the mount


@dataclass
class EntryStatus:
    id: str
    name: str
    protocol: str                  # "torrent" | "nzb"
    status: str                    # "queued" | "downloading" | "ready" | "error"
    progress: float = 0.0          # 0..100
    error: str = ""
    files: list[EngineFile] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict) -> "EntryStatus":
        return cls(id=d["id"], name=d.get("name", ""), protocol=d.get("protocol", ""),
                   status=d.get("status", "queued"), progress=d.get("progress", 0.0),
                   error=d.get("error", ""),
                   files=[EngineFile(f["name"], f.get("size", 0), f.get("path", ""))
                          for f in d.get("files") or []])


async def _call(method: str, path: str, **kw):
    try:
        resp = await http.request(method, f"{ENGINE_URL}{path}", retries=1, timeout=60, **kw)
    except Exception as e:
        raise EngineDown(f"engine unreachable: {e}") from e
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", resp.text)
        except ValueError:
            msg = resp.text
        raise EngineError(f"engine {method} {path}: {resp.status_code} {msg}")
    return resp.json() if resp.content else {}


async def status() -> dict:
    return await _call("GET", "/status") or {}


async def streams() -> dict:
    """Files being read through the mount right now + usenet connection use."""
    return await _call("GET", "/streams") or {}


async def queue(limit: int = 300) -> dict:
    """cli_mount queue page data: entries being added, newest mount entries, totals."""
    return await _call("GET", "/queue", params={"limit": limit}) or {}


async def cached(hashes: list[str]) -> dict[str, bool]:
    if not hashes:
        return {}
    out: dict[str, bool] = {}
    for i in range(0, len(hashes), 100):
        chunk = hashes[i:i + 100]
        out.update(await _call("GET", "/cache", params={"hashes": ",".join(chunk)}) or {})
    return out


async def add_magnet(magnet: str) -> str:
    r = await _call("POST", "/add", json={"magnet": magnet})
    return r["id"]


async def add_nzb(nzb: bytes, name: str) -> str:
    r = await _call("POST", "/add", json={"nzb_b64": base64.b64encode(nzb).decode(), "name": name})
    return r["id"]


async def entry(entry_id: str) -> EntryStatus | None:
    d = await _call("GET", f"/entries/{entry_id}")
    return EntryStatus.from_json(d) if d else None


async def delete(entry_id: str) -> None:
    await _call("DELETE", f"/entries/{entry_id}")


async def check_health(entry_id: str) -> str:
    r = await _call("POST", f"/entries/{entry_id}/health")
    return (r or {}).get("status", "unknown")


async def health(entry_id: str) -> str:
    r = await _call("GET", f"/entries/{entry_id}/health")
    return (r or {}).get("status", "unknown")
