"""Endpoint — EV charging stations (Open Charge Map).

Wrapper de l'API Open Charge Map : prend un point (latitude, longitude) et un
rayon, renvoie les bornes de recharge alentour avec opérateur, connecteurs
(type, puissance kW, courant), nombre de points et statut.

Source : Open Charge Map (api.openchargemap.io), clé requise.
Tier "verif" $0.01. TTL 1h (le parc de bornes évolue lentement ; la dispo
temps réel n'est pas garantie par OCM).
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import OPENCHARGEMAP_API_KEY

router = APIRouter()

OCM_URL = "https://api.openchargemap.io/v3/poi"
SOURCE_NAME = "Open Charge Map (api.openchargemap.io)"

_CACHE_TTL = 3600  # 1h
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(10.0, connect=4.0)
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


def _shape_poi(poi: dict[str, Any]) -> dict[str, Any]:
    addr = poi.get("AddressInfo") or {}
    operator = poi.get("OperatorInfo") or {}
    status = poi.get("StatusType") or {}
    connections = []
    for conn in poi.get("Connections") or []:
        ctype = conn.get("ConnectionType") or {}
        cstatus = conn.get("StatusType") or {}
        connections.append({
            "type": ctype.get("Title"),
            "power_kw": conn.get("PowerKW"),
            "current_type_id": conn.get("CurrentTypeID"),
            "quantity": conn.get("Quantity"),
            "status": cstatus.get("Title"),
            "operational": cstatus.get("IsOperational"),
        })

    return {
        "id": poi.get("ID"),
        "title": addr.get("Title"),
        "address": addr.get("AddressLine1"),
        "town": addr.get("Town"),
        "postcode": addr.get("Postcode"),
        "country_id": addr.get("CountryID"),
        "latitude": addr.get("Latitude"),
        "longitude": addr.get("Longitude"),
        "distance_km": addr.get("Distance"),
        "operator": operator.get("Title"),
        "usage_cost": poi.get("UsageCost"),
        "num_points": poi.get("NumberOfPoints"),
        "status": status.get("Title"),
        "operational": status.get("IsOperational"),
        "connections": connections,
        "date_last_verified": poi.get("DateLastVerified"),
    }


async def _fetch_poi(params: dict[str, Any]) -> list[dict[str, Any]]:
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(OCM_URL, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        return data
                    raise HTTPException(status_code=502, detail="Réponse Open Charge Map inattendue.")
                if resp.status_code in (401, 403):
                    raise HTTPException(status_code=502, detail="Clé Open Charge Map refusée côté serveur.")
                last_exc = httpx.HTTPStatusError(
                    f"OCM HTTP {resp.status_code}", request=resp.request, response=resp
                )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Open Charge Map.")
    raise HTTPException(status_code=502, detail="Service Open Charge Map indisponible.")


async def lookup_charging(
    latitude: float, longitude: float, distance: float, maxresults: int
) -> dict[str, Any]:
    if not (-90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="Latitude hors bornes [-90, 90].")
    if not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="Longitude hors bornes [-180, 180].")
    if not (0 < distance <= 200):
        raise HTTPException(status_code=400, detail="Rayon 'distance' attendu dans ]0, 200] km.")
    if not (1 <= maxresults <= 200):
        raise HTTPException(status_code=400, detail="'maxresults' attendu dans [1, 200].")

    key = f"{latitude:.4f},{longitude:.4f},{distance},{maxresults}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    pois = await _fetch_poi({
        "key": OPENCHARGEMAP_API_KEY,
        "latitude": latitude,
        "longitude": longitude,
        "distance": distance,
        "distanceunit": "KM",
        "maxresults": maxresults,
        "output": "json",
        "compact": "true",
        "verbose": "false",
    })
    shaped = {
        "query": {"latitude": latitude, "longitude": longitude, "distance_km": distance},
        "count": len(pois),
        "stations": [_shape_poi(p) for p in pois],
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/ev/charging")
async def ev_charging(
    latitude: float = Query(..., description="Latitude of search center, e.g. 48.85"),
    longitude: float = Query(..., description="Longitude of search center, e.g. 2.35"),
    distance: float = Query(5.0, description="Search radius in km (0-200], e.g. 5"),
    maxresults: int = Query(20, description="Max stations to return [1-200], e.g. 20"),
) -> JSONResponse:
    """GET /ev/charging?latitude=&longitude=&distance=&maxresults= — bornes de recharge alentour."""
    data = await lookup_charging(latitude, longitude, distance, maxresults)
    return JSONResponse(content=data)


@router.get("/ev/health")
async def ev_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
            r = await client.get(OCM_URL, params={
                "key": OPENCHARGEMAP_API_KEY, "latitude": 48.85, "longitude": 2.35,
                "distance": 3, "maxresults": 1, "output": "json", "compact": "true",
            })
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "ev",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
