"""One shared async HTTP client with small, sane retries.

Retries 429 and 5xx (honouring Retry-After up to 30s) and connection errors.
Everything else is returned to the caller as-is.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from . import __version__

log = logging.getLogger("clid2.http")

USER_AGENT = f"cli_debrid/{__version__}"
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class HTTPError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


async def request(method: str, url: str, *, retries: int = 3, **kw: Any) -> httpx.Response:
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client().request(method, url, **kw)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_exc = HTTPError(resp.status_code, url, resp.text)
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = min(float(ra), 30.0)
        if attempt < retries:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    assert last_exc is not None
    raise last_exc


async def get_json(url: str, **kw: Any) -> Any:
    resp = await request("GET", url, **kw)
    if resp.status_code >= 400:
        raise HTTPError(resp.status_code, url, resp.text)
    return resp.json()
