"""Endpoint — Flights : états d'aéronefs en temps réel (adsb.fi open data).

Wrapper de l'API open data adsb.fi (format compatible ADSBexchange v2) : prend une
bounding box (lamin, lomin, lamax, lomax) et renvoie les aéronefs présents dans la
zone avec position, altitude, vitesse, cap, etc.

⚠️ Source MIGRÉE depuis OpenSky vers adsb.fi : OpenSky bloque les IP datacenter
(serveur d'auth injoignable depuis l'hébergeur). adsb.fi est publique, sans clé,
et compatible cloud. Interface/réponse conservées (mêmes champs) pour ne rien
casser côté agents/discovery.

Source : adsb.fi open data (opendata.adsb.fi), communautaire, licence ODbL.
Attribution obligatoire (créditée dans la réponse). Rate limit : 1 req/s.
Tier "verif" $0.01. TTL très court (15s) — positions temps réel.

adsb.fi étant en point+rayon, on convertit la bbox en centre + rayon (NM) puis on
filtre les aéronefs réellement dans la bbox (rayon max adsb.fi ~ 250 NM).
"""

import asyncio
import math
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

ADSBFI_BASE = "https://opendata.adsb.fi/api/v3"
SOURCE_NAME = "adsb.fi open data (opendata.adsb.fi)"
ATTRIBUTION = "Live ADS-B data by adsb.fi (https://adsb.fi) — community ODbL, free open data."

_MAX_RADIUS_NM = 250          # rayon max supporté par adsb.fi
_NM_PER_DEG_LAT = 60.0        # 1° de latitude ≈ 60 NM

# Conversions vers les unités de l'ancien schéma (mètres, m/s) — interface inchangée.
_FT_TO_M = 0.3048
_KT_TO_MS = 0.514444
_FTMIN_TO_MS = 0.00508

_CACHE_TTL = 15  # 15s
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Rate limit adsb.fi : 1 req/s, pas de retry agressif. 1 seule retentative douce sur
# erreur réseau transitoire, espacée de > 1s.
_TIMEOUT = httpx.Timeout(12.0, connect=5.0)
_RETRY_DELAY = 1.2
_HEADERS = {"User-Agent": "x402-endpoints/1.0 (flights via adsb.fi)"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _validate_bbox(lamin: float, lomin: float, lamax: float, lomax: float) -> float:
    if not (-90 <= lamin <= 90 and -90 <= lamax <= 90):
        raise HTTPException(status_code=400, detail="Latitudes hors bornes [-90, 90].")
    if not (-180 <= lomin <= 180 and -180 <= lomax <= 180):
        raise HTTPException(status_code=400, detail="Longitudes hors bornes [-180, 180].")
    if lamin >= lamax or lomin >= lomax:
        raise HTTPException(status_code=400, detail="Bounding box invalide : min doit être < max.")
    # Rayon (NM) du centre de la bbox jusqu'à un coin.
    clat = (lamin + lamax) / 2.0
    dlat_nm = (lamax - lamin) / 2.0 * _NM_PER_DEG_LAT
    dlon_nm = (lomax - lomin) / 2.0 * _NM_PER_DEG_LAT * math.cos(math.radians(clat))
    radius_nm = math.hypot(dlat_nm, dlon_nm)
    if radius_nm > _MAX_RADIUS_NM:
        raise HTTPException(
            status_code=400,
            detail=f"Bounding box trop large (rayon {radius_nm:.0f} NM depuis le centre > {_MAX_RADIUS_NM} NM "
                   "supporté par adsb.fi). Réduire la zone.")
    return radius_nm


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _shape_aircraft(a: dict[str, Any], now_s: float) -> dict[str, Any]:
    alt_baro_raw = a.get("alt_baro")
    on_ground = alt_baro_raw == "ground"
    baro_ft = _num(alt_baro_raw) if not on_ground else None
    geom_ft = _num(a.get("alt_geom"))
    gs_kt = _num(a.get("gs"))
    vrate_ftmin = _num(a.get("baro_rate"))
    if vrate_ftmin is None:
        vrate_ftmin = _num(a.get("geom_rate"))
    callsign = a.get("flight")
    seen_pos = _num(a.get("seen_pos"))
    last_contact = int(now_s - seen_pos) if (seen_pos is not None) else None
    return {
        "icao24": a.get("hex"),
        "callsign": callsign.strip() if isinstance(callsign, str) else None,
        "origin_country": None,  # non fourni par adsb.fi (champ conservé pour compat schéma)
        "longitude": _num(a.get("lon")),
        "latitude": _num(a.get("lat")),
        "baro_altitude_m": round(baro_ft * _FT_TO_M, 1) if baro_ft is not None else None,
        "geo_altitude_m": round(geom_ft * _FT_TO_M, 1) if geom_ft is not None else None,
        "velocity_ms": round(gs_kt * _KT_TO_MS, 2) if gs_kt is not None else None,
        "true_track_deg": _num(a.get("track")),
        "vertical_rate_ms": round(vrate_ftmin * _FTMIN_TO_MS, 2) if vrate_ftmin is not None else None,
        "on_ground": on_ground,
        "squawk": a.get("squawk"),
        "last_contact": last_contact,
        "registration": a.get("r"),  # bonus adsb.fi (immatriculation)
        "aircraft_type": a.get("t"),  # bonus adsb.fi (code type, ex. A320)
    }


async def _fetch_adsbfi(client: httpx.AsyncClient, clat: float, clon: float, radius_nm: int) -> dict[str, Any]:
    url = f"{ADSBFI_BASE}/lat/{clat:.5f}/lon/{clon:.5f}/dist/{radius_nm}"
    last_exc: Exception | None = None
    for attempt in range(2):  # 1 essai + 1 retry doux (pas agressif, > 1s)
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                raise HTTPException(status_code=503, detail="adsb.fi : limite de débit atteinte (1 req/s), réessayer plus tard.")
            last_exc = httpx.HTTPStatusError(
                f"adsb.fi HTTP {resp.status_code}", request=resp.request, response=resp)
        if attempt == 0:
            await asyncio.sleep(_RETRY_DELAY)
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant adsb.fi.")
    raise HTTPException(status_code=502, detail="Service adsb.fi indisponible.")


async def lookup_states(lamin: float, lomin: float, lamax: float, lomax: float) -> dict[str, Any]:
    radius_nm = _validate_bbox(lamin, lomin, lamax, lomax)
    key = f"{lamin},{lomin},{lamax},{lomax}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    clat = (lamin + lamax) / 2.0
    clon = (lomin + lomax) / 2.0
    query_radius = max(1, min(_MAX_RADIUS_NM, math.ceil(radius_nm) + 1))

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        raw = await _fetch_adsbfi(client, clat, clon, query_radius)

    now_s = (raw.get("now") or time.time() * 1000) / 1000.0
    aircraft = raw.get("ac") or []
    # Filtrer au strict de la bbox (adsb.fi renvoie un disque ⊇ bbox).
    states = []
    for a in aircraft:
        lat, lon = _num(a.get("lat")), _num(a.get("lon"))
        if lat is None or lon is None:
            continue
        if lamin <= lat <= lamax and lomin <= lon <= lomax:
            states.append(_shape_aircraft(a, now_s))

    shaped = {
        "bbox": {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax},
        "time": int(now_s),
        "count": len(states),
        "states": states,
        "source": SOURCE_NAME,
        "attribution": ATTRIBUTION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/flights/states")
async def flights_states(
    lamin: float = Query(..., description="Min latitude of bounding box, e.g. 48.0"),
    lomin: float = Query(..., description="Min longitude of bounding box, e.g. 2.0"),
    lamax: float = Query(..., description="Max latitude of bounding box, e.g. 49.0"),
    lomax: float = Query(..., description="Max longitude of bounding box, e.g. 3.0"),
) -> JSONResponse:
    """GET /flights/states?lamin=&lomin=&lamax=&lomax= — aéronefs temps réel dans une zone (adsb.fi)."""
    data = await lookup_states(lamin, lomin, lamax, lomax)
    return JSONResponse(content=data)


@router.get("/flights/health")
async def flights_health() -> JSONResponse:
    """Santé : ping léger adsb.fi (1 petit appel, respecte le rate limit)."""
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), headers=_HEADERS) as client:
            r = await client.get(f"{ADSBFI_BASE}/lat/48.85/lon/2.35/dist/5")
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "flights",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "attribution": ATTRIBUTION,
            "cache_entries": len(_cache),
        },
    )
