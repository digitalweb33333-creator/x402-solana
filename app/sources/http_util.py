"""Shared HTTP helper for multi-source endpoints (crypto pre-trade).

Provides: named keep-alive httpx clients, GET/POST JSON with retry+backoff that
NEVER RAISE (return (data, None) or (None, "error")), and a simple TTL cache.

"Never an error after payment" principle: endpoints aggregate several sources via
these helpers; as long as AT LEAST one source responds, the handler returns 200
(best-effort) and the payment settles. If EVERYTHING fails, the handler raises an
HTTPException (502/504) -> the x402 middleware does NOT settle (response >= 400) ->
the agent is not charged.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

# Browser-like headers: some public APIs return 403 to non-browser UAs from
# datacenter IPs (lesson from the NIH/Gazette endpoints blocked on Render).
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_clients: dict[str, httpx.AsyncClient] = {}
_clients_lock = asyncio.Lock()


async def client(key: str = "default", *, timeout: float = 12.0, connect: float = 4.0,
                 headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """Shared, named httpx client (connection kept warm per source)."""
    c = _clients.get(key)
    if c is None or c.is_closed:
        async with _clients_lock:
            c = _clients.get(key)
            if c is None or c.is_closed:
                c = httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=connect),
                    headers=headers or BROWSER_HEADERS,
                    limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
                    follow_redirects=True,
                )
                _clients[key] = c
    return c


async def get_json(c: httpx.AsyncClient, url: str, *, params: dict | None = None,
                   headers: dict | None = None, attempts: int = 3, backoff: float = 0.5
                   ) -> tuple[Any, str | None]:
    """GET JSON with retry. Returns (data, None) or (None, 'error'). Never raises."""
    last = "unknown"
    for attempt in range(attempts):
        try:
            r = await c.get(url, params=params, headers=headers)
            if r.status_code == 200:
                try:
                    return r.json(), None
                except Exception:
                    return None, "invalid_json"
            if r.status_code == 404:
                return None, "not_found"
            last = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code not in (408, 429, 451):
                return None, last  # non-retryable client error
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, last


async def post_json(c: httpx.AsyncClient, url: str, *, json: dict | None = None,
                    headers: dict | None = None, attempts: int = 3, backoff: float = 0.5
                    ) -> tuple[Any, str | None]:
    """POST JSON with retry. Returns (data, None) or (None, 'error'). Never raises."""
    last = "unknown"
    for attempt in range(attempts):
        try:
            r = await c.post(url, json=json, headers=headers)
            if r.status_code == 200:
                try:
                    return r.json(), None
                except Exception:
                    return None, "invalid_json"
            last = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code not in (408, 429, 451):
                return None, last
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, last


async def get_text(c: httpx.AsyncClient, url: str, *, headers: dict | None = None,
                   attempts: int = 2, backoff: float = 0.5) -> tuple[str | None, str | None]:
    """GET texte brut (ex. markdown). Renvoie (text, None) ou (None, 'erreur')."""
    last = "unknown"
    for attempt in range(attempts):
        try:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                return r.text, None
            last = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code not in (408, 429, 451):
                return None, last
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, last


class TTLCache:
    """Simple in-memory cache with per-entry TTL."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.time() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any) -> None:
        self._d[key] = (time.time(), val)

    def __len__(self) -> int:
        return len(self._d)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
