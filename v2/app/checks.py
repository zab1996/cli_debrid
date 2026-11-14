"""Connection tests for the setup wizard and settings page.

Each returns "" when OK, else a short human-readable problem.
"""
from __future__ import annotations

import asyncio
import ssl

from . import http
from .config import IndexerConfig, UsenetProvider

DEBRID_CHECK = {
    "realdebrid": ("https://api.real-debrid.com/rest/1.0/user", "bearer"),
    "torbox": ("https://api.torbox.app/v1/api/user/me", "bearer"),
    "alldebrid": ("https://api.alldebrid.com/v4/user?agent=cli_debrid", "bearer"),
    "premiumize": ("https://www.premiumize.me/api/account/info", "query:apikey"),
    "debridlink": ("https://debrid-link.com/api/v2/account/infos", "bearer"),
}


async def _ok(url: str, **kw) -> str:
    try:
        resp = await http.request("GET", url, retries=0, timeout=15, **kw)
    except Exception as e:
        return f"can't connect ({type(e).__name__})"
    if resp.status_code in (401, 403):
        return "the key was rejected"
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}"
    return ""


async def tmdb(key: str) -> str:
    if not key:
        return "missing key"
    if len(key) > 40:
        return await _ok("https://api.themoviedb.org/3/configuration",
                         headers={"Authorization": f"Bearer {key}"})
    return await _ok("https://api.themoviedb.org/3/configuration", params={"api_key": key})


async def tvdb(key: str) -> str:
    if not key:
        return ""  # optional
    try:
        resp = await http.request("POST", "https://api4.thetvdb.com/v4/login",
                                  json={"apikey": key}, retries=0, timeout=15)
    except Exception as e:
        return f"can't connect ({type(e).__name__})"
    return "" if resp.status_code == 200 else "the key was rejected"


async def debrid(provider: str, key: str) -> str:
    if not provider:
        return ""
    if not key:
        return "missing API key"
    url, auth = DEBRID_CHECK[provider]
    if auth == "bearer":
        return await _ok(url, headers={"Authorization": f"Bearer {key}"})
    return await _ok(url, params={auth.split(":")[1]: key})


async def usenet(p: UsenetProvider) -> str:
    """Connect, read the greeting and log in (NNTP AUTHINFO)."""
    try:
        ctx = ssl.create_default_context() if p.ssl else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(p.host, p.port, ssl=ctx, server_hostname=p.host if ctx else None),
            timeout=15)
    except Exception as e:
        return f"can't connect to {p.host}:{p.port} ({type(e).__name__})"
    try:
        greet = (await asyncio.wait_for(reader.readline(), 15)).decode(errors="replace")
        if not greet.startswith(("200", "201")):
            return f"unexpected greeting: {greet.strip()[:60]}"
        if p.username:
            writer.write(f"AUTHINFO USER {p.username}\r\n".encode())
            await writer.drain()
            line = (await asyncio.wait_for(reader.readline(), 15)).decode(errors="replace")
            if line.startswith("381"):
                writer.write(f"AUTHINFO PASS {p.password}\r\n".encode())
                await writer.drain()
                line = (await asyncio.wait_for(reader.readline(), 15)).decode(errors="replace")
            if not line.startswith("281"):
                return "login failed (check username/password)"
        writer.write(b"QUIT\r\n")
        await writer.drain()
        return ""
    except Exception as e:
        return f"error talking to server ({type(e).__name__})"
    finally:
        writer.close()


async def indexer(idx: IndexerConfig) -> str:
    base = (idx.url or "").rstrip("/")
    if idx.type == "torrentio":
        return await _ok(f"{base or 'https://torrentio.strem.fun'}/manifest.json")
    if not base:
        return "missing URL"
    if idx.type == "zilean":
        return await _ok(f"{base}/healthchecks/ping")
    if idx.type == "prowlarr":
        return await _ok(f"{base}/api/v1/system/status", headers={"X-Api-Key": idx.api_key})
    if idx.type == "jackett":
        return await _ok(f"{base}/api/v2.0/indexers/all/results",
                         params={"apikey": idx.api_key, "Query": "test"})
    if idx.type == "newznab":
        endpoint = base if base.endswith("/api") else f"{base}/api"
        try:
            resp = await http.request("GET", endpoint, params={"t": "caps", "apikey": idx.api_key},
                                      retries=0, timeout=15)
        except Exception as e:
            return f"can't connect ({type(e).__name__})"
        if resp.status_code >= 400 or "<error" in resp.text[:300]:
            return "the indexer rejected the request (check the API key)"
        return ""
    return "unknown indexer type"
