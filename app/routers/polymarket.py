"""Endpoint — Polymarket live odds & probabilities.

Wrapper de l'API Gamma publique de Polymarket : prend un identifiant de marché
(id numérique ou slug) et renvoie les issues (outcomes) avec leur probabilité
implicite (prix 0-1), plus le volume/liquidité et l'état du marché.

Source : Polymarket Gamma API (gamma-api.polymarket.com), publique, sans clé.
Tier "verdict" $0.05. TTL très court (20s) — données de marché temps réel.

Note source : `outcomes` et `outcomePrices` arrivent comme des STRINGS JSON
(ex. '["Yes","No"]', '["0.06","0.94"]') correspondant 1:1 ; prix = probabilité 0-1.
"""

import asyncio
import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
SOURCE_NAME = "Polymarket Gamma API (gamma-api.polymarket.com)"
DISCLAIMER = "Cotes de marché indicatives, pas un conseil en investissement."

_CACHE_TTL = 20  # 20s, temps réel
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _parse_json_array(value: Any) -> list[Any]:
    """outcomes/outcomePrices peuvent être des strings JSON ou déjà des listes."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _shape(market: dict[str, Any]) -> dict[str, Any]:
    names = _parse_json_array(market.get("outcomes"))
    prices = _parse_json_array(market.get("outcomePrices"))
    outcomes = []
    for i, name in enumerate(names):
        price = prices[i] if i < len(prices) else None
        try:
            prob = float(price) if price is not None else None
        except (TypeError, ValueError):
            prob = None
        outcomes.append({"name": name, "price": prob, "probability": prob})

    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "outcomes": outcomes,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "volume": market.get("volume"),
        "liquidity": market.get("liquidity"),
        "end_date": market.get("endDate"),
        "resolution_source": market.get("resolutionSource") or None,
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }


async def _request(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Polymarket.")
    raise HTTPException(status_code=502, detail="API Polymarket indisponible.")


async def _fetch_market(market: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        if market.isdigit():
            # id numérique -> /markets/{id} renvoie un objet
            resp = await _request(client, f"{GAMMA_BASE}/{market}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Marché Polymarket introuvable pour cet id.")
            if resp.status_code == 200:
                data = resp.json()
                return data[0] if isinstance(data, list) else data
        else:
            # slug -> /markets?slug= renvoie une liste
            resp = await _request(client, GAMMA_BASE, params={"slug": market})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                raise HTTPException(status_code=404, detail="Marché Polymarket introuvable pour ce slug.")
        raise HTTPException(status_code=502, detail=f"Réponse Polymarket inattendue (HTTP {resp.status_code}).")


async def lookup_market(market: str | None) -> dict[str, Any]:
    m = (market or "").strip()
    if not m or len(m) > 200 or "/" in m:
        raise HTTPException(status_code=400, detail="Paramètre 'market' requis : id numérique ou slug Polymarket.")

    cached = _cache_get(m)
    if cached is not None:
        return {**cached, "cached": True}

    raw = await _fetch_market(m)
    shaped = _shape(raw)
    _cache[m] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/polymarket/odds")
async def polymarket_odds(
    market: str = Query(
        ...,
        description="Polymarket market id or slug, e.g. '2654605' or 'will-it-rain-tomorrow'",
    ),
) -> JSONResponse:
    """GET /polymarket/odds?market= — cotes/probabilités live d'un marché Polymarket."""
    data = await lookup_market(market)
    return JSONResponse(content=data)


@router.get("/polymarket/health")
async def polymarket_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
            r = await client.get(GAMMA_BASE, params={"limit": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "polymarket",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
