"""Endpoint — Polymarket resolution oracle (résolution de marché).

DISTINCT de l'endpoint odds existant (app/routers/polymarket.py, /polymarket/odds
= cotes live). Ici on renvoie l'état de RÉSOLUTION d'un marché : closed (bool),
outcome gagnant dérivé honnêtement des outcomePrices (1/0), source de résolution,
endDate, condition_id/slug. Permet aussi de lister les marchés résolus récents.

Source : Polymarket Gamma API (gamma-api.polymarket.com), read-only, on-chain
indexé, sans authentification.

Honnêteté (cf description) : Gamma reporte parfois des marchés (sport) comme
active/closed de façon incohérente — on renvoie les champs BRUTS et on ne dérive
le gagnant QUE si les outcomePrices sont sans ambiguïté (un 1, le reste 0). Sinon
winner = null + statut "indeterminate". Pas d'invention.

Tier $0.01. TTL : marché actif -> court (5 min) ; marché clos -> long (résolution permanente).
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
DISCLAIMER = "Statut de résolution indicatif issu de Polymarket (on-chain indexé), pas un conseil en investissement."

_CACHE_TTL_OPEN = 5 * 60          # marché actif : court
_CACHE_TTL_CLOSED = 7 * 24 * 3600  # marché clos : résolution permanente
_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}  # key -> (ts, ttl, data)

_TIMEOUT = httpx.Timeout(12.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_HEADERS = {"User-Agent": "x402-endpoints/1.0 (polymarket resolution)"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, ttl, data = hit
    if time.time() - ts > ttl:
        _cache.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: dict[str, Any], closed: bool) -> None:
    ttl = _CACHE_TTL_CLOSED if closed else _CACHE_TTL_OPEN
    _cache[key] = (time.time(), ttl, data)


def _parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _derive_winner(outcomes: list[Any], prices: list[Any], closed: bool) -> dict[str, Any]:
    """Dérive le gagnant UNIQUEMENT si non ambigu (un prix == 1, le reste == 0)."""
    parsed_prices: list[float | None] = []
    for p in prices:
        try:
            parsed_prices.append(float(p))
        except (TypeError, ValueError):
            parsed_prices.append(None)

    pairs = [
        {"outcome": outcomes[i] if i < len(outcomes) else None, "price": parsed_prices[i]}
        for i in range(max(len(outcomes), len(parsed_prices)))
    ]

    winner = None
    status = "unresolved"
    if not closed:
        status = "open"
    else:
        ones = [i for i, p in enumerate(parsed_prices) if p is not None and abs(p - 1.0) < 1e-9]
        zeros = [p for p in parsed_prices if p is not None and abs(p) < 1e-9]
        if len(ones) == 1 and len(zeros) == len(parsed_prices) - 1:
            idx = ones[0]
            winner = outcomes[idx] if idx < len(outcomes) else None
            status = "resolved"
        elif parsed_prices and all(p is not None and abs(p) < 1e-9 for p in parsed_prices):
            status = "indeterminate"  # ex. marché annulé/voided (tous prix à 0)
        else:
            status = "indeterminate"  # prix fractionnaires : pas de gagnant net
    return {
        "resolution_status": status,
        "winning_outcome": winner,
        "outcomes": pairs,
    }


def _shape(market: dict[str, Any]) -> dict[str, Any]:
    outcomes = _parse_json_array(market.get("outcomes"))
    prices = _parse_json_array(market.get("outcomePrices"))
    closed = bool(market.get("closed"))
    derived = _derive_winner(outcomes, prices, closed)
    return {
        "question": market.get("question"),
        "slug": market.get("slug"),
        "condition_id": market.get("conditionId"),
        "closed": closed,
        "active": market.get("active"),
        "resolution_status": derived["resolution_status"],
        "winning_outcome": derived["winning_outcome"],
        "outcomes": derived["outcomes"],
        "outcome_prices_raw": market.get("outcomePrices"),
        "resolution_source": market.get("resolutionSource") or None,
        "uma_resolution_status": market.get("umaResolutionStatus") or market.get("umaResolutionStatuses") or None,
        "end_date": market.get("endDate"),
        "closed_time": market.get("closedTime"),
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }


async def _request(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 429:
                last_exc = httpx.HTTPStatusError("Gamma 429", request=resp.request, response=resp)
            else:
                return resp
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Polymarket Gamma.")
    if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code == 429:
        raise HTTPException(status_code=503, detail="Polymarket Gamma rate-limited (429), réessayer.")
    raise HTTPException(status_code=502, detail="API Polymarket Gamma indisponible.")


async def lookup_resolution(slug: str | None, condition_id: str | None, limit: int) -> dict[str, Any]:
    slug = (slug or "").strip()
    condition_id = (condition_id or "").strip()
    if slug and ("/" in slug or len(slug) > 250):
        raise HTTPException(status_code=400, detail="'slug' invalide.")
    if condition_id and not (condition_id.startswith("0x") and len(condition_id) <= 80):
        raise HTTPException(status_code=400, detail="'condition_id' attendu au format 0x… (hex).")
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        # --- Lookup ciblé par slug ou condition_id ---
        if slug or condition_id:
            cache_key = f"slug={slug}" if slug else f"cond={condition_id}"
            cached = _cache_get(cache_key)
            if cached is not None:
                return {**cached, "cached": True}
            base_params = {"slug": slug} if slug else {"condition_ids": condition_id}
            # Gamma exclut par défaut les marchés clos du filtre slug/condition : on
            # interroge d'abord le défaut (marchés actifs), puis on retente avec
            # closed=true si vide (marchés résolus). Même endpoint officiel, pas de contournement.
            items: list[Any] = []
            for extra in ({}, {"closed": "true"}):
                resp = await _request(client, GAMMA_BASE, params={**base_params, **extra})
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"Réponse Polymarket inattendue (HTTP {resp.status_code}).")
                data = resp.json()
                items = data if isinstance(data, list) else ([data] if data else [])
                if items:
                    break
            if not items:
                raise HTTPException(
                    status_code=404,
                    detail="Marché introuvable pour ce slug/condition_id.")
            shaped = _shape(items[0])
            result = {"mode": "lookup", "market": shaped,
                      "source": SOURCE_NAME, "timestamp": shaped["timestamp"]}
            _cache_set(cache_key, result, shaped["closed"])
            return {**result, "cached": False}

        # --- Listing des marchés résolus récents ---
        cache_key = f"closed-list|{limit}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
        resp = await _request(client, GAMMA_BASE, params={
            "closed": "true", "limit": limit, "order": "endDate", "ascending": "false"})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Réponse Polymarket inattendue (HTTP {resp.status_code}).")
        data = resp.json()
        items = data if isinstance(data, list) else []
        markets = [_shape(m) for m in items]
        result = {
            "mode": "resolved_list",
            "count": len(markets),
            "markets": markets,
            "source": SOURCE_NAME,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "disclaimer": DISCLAIMER,
        }
        _cache_set(cache_key, result, closed=True)
        return {**result, "cached": False}


@router.get("/polymarket/resolution")
async def polymarket_resolution(
    slug: str | None = Query(None, description="Polymarket market slug, e.g. 'new-rihanna-album-before-gta-vi-926'"),
    condition_id: str | None = Query(None, description="On-chain condition id, e.g. '0x1fad72...'"),
    limit: int = Query(20, description="Max markets when listing resolved markets [1-50], e.g. 20"),
) -> JSONResponse:
    """GET /polymarket/resolution — statut de résolution + outcome gagnant (Gamma API). Distinct de /polymarket/odds."""
    data = await lookup_resolution(slug, condition_id, limit)
    return JSONResponse(content=data)


@router.get("/polymarket/resolution/health")
async def polymarket_resolution_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers=_HEADERS) as client:
            r = await client.get(GAMMA_BASE, params={"closed": "true", "limit": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "polymarket-resolution",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
