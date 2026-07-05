"""Endpoint — Google Solar (potentiel solaire d'un bâtiment).

Wrapper de l'API officielle Google Maps Platform Solar (buildingInsights) : pour
une coordonnée, renvoie une synthèse propre du potentiel solaire du bâtiment le
plus proche — nombre max de panneaux, surface de toit exploitable, heures
d'ensoleillement/an, facteur de compensation carbone et surface totale de toit.

Source : Google Maps Platform Solar API (solar.googleapis.com), clé API en query.
Tier $0.05 (donnée propriétaire à forte valeur). TTL 30 jours (imagerie stable
mais réactualisée périodiquement par Google).
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import GOOGLE_SOLAR_API_KEY

router = APIRouter()

SOLAR_URL = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
SOURCE_NAME = "Google Maps Platform Solar API (solar.googleapis.com)"
ATTRIBUTION = "Solar data © Google — Google Maps Platform Solar API"

_QUALITIES = {"HIGH", "MEDIUM", "BASE"}
_CACHE_TTL = 30 * 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_HEADERS = {"User-Agent": "x402-endpoints/1.0"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _fmt_date(d: dict[str, Any] | None) -> str | None:
    if not isinstance(d, dict) or not d.get("year"):
        return None
    return f"{d['year']:04d}-{int(d.get('month', 1)):02d}-{int(d.get('day', 1)):02d}"


def _shape(payload: dict[str, Any]) -> dict[str, Any]:
    sp = payload.get("solarPotential", {}) or {}
    whole = sp.get("wholeRoofStats", {}) or {}
    return {
        "name": payload.get("name"),
        "center": payload.get("center"),
        "imagery_quality": payload.get("imageryQuality"),
        "imagery_date": _fmt_date(payload.get("imageryDate")),
        "postal_code": payload.get("postalCode"),
        "administrative_area": payload.get("administrativeArea"),
        "region_code": payload.get("regionCode"),
        "solar_potential": {
            "max_array_panels_count": sp.get("maxArrayPanelsCount"),
            "max_array_area_meters2": sp.get("maxArrayAreaMeters2"),
            "max_sunshine_hours_per_year": sp.get("maxSunshineHoursPerYear"),
            "carbon_offset_factor_kg_per_mwh": sp.get("carbonOffsetFactorKgPerMwh"),
            "panel_capacity_watts": sp.get("panelCapacityWatts"),
            "whole_roof_area_meters2": whole.get("areaMeters2"),
        },
        "source": SOURCE_NAME,
        "attribution": ATTRIBUTION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def building_insights(lat: float | None, lon: float | None, quality: str | None) -> dict[str, Any]:
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="'lat' et 'lon' sont requis.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="'lat'/'lon' hors bornes.")
    q = (quality or "HIGH").strip().upper()
    if q not in _QUALITIES:
        raise HTTPException(status_code=400, detail="'quality' attendu: HIGH | MEDIUM | BASE.")

    cache_key = f"{round(lat, 6)}|{round(lon, 6)}|{q}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    params = {
        "location.latitude": lat, "location.longitude": lon,
        "requiredQuality": q, "key": GOOGLE_SOLAR_API_KEY,
    }
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(SOLAR_URL, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    shaped = _shape(resp.json())
                    _cache[cache_key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                if resp.status_code == 404:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Aucun bâtiment solaire trouvé à cette coordonnée (qualité {q}). "
                               "Couverture variable selon le pays ; réessayer avec quality=MEDIUM ou BASE.")
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête Google Solar invalide (coordonnée/paramètre).")
                if resp.status_code in (403, 429):
                    raise HTTPException(status_code=503, detail="Quota Google Solar dépassé ou accès refusé, réessayer plus tard.")
                last_exc = httpx.HTTPStatusError(
                    f"Solar HTTP {resp.status_code}", request=resp.request, response=resp)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Google Solar.")
    raise HTTPException(status_code=502, detail="Service Google Solar indisponible.")


@router.get("/solar/building-insights")
async def solar_building_insights(
    lat: float | None = Query(None, description="Latitude of the building, e.g. 48.139"),
    lon: float | None = Query(None, description="Longitude of the building, e.g. 11.566"),
    quality: str | None = Query(None, description="Required imagery quality: HIGH | MEDIUM | BASE (default HIGH)"),
) -> JSONResponse:
    """GET /solar/building-insights — potentiel solaire d'un bâtiment (Google Solar API)."""
    data = await building_insights(lat, lon, quality)
    return JSONResponse(content=data)


@router.get("/solar/health")
async def solar_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers=_HEADERS) as client:
            r = await client.get(SOLAR_URL, params={
                "location.latitude": 48.139, "location.longitude": 11.566,
                "requiredQuality": "HIGH", "key": GOOGLE_SOLAR_API_KEY})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "solar-building-insights",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(GOOGLE_SOLAR_API_KEY),
            "cache_entries": len(_cache),
        },
    )
