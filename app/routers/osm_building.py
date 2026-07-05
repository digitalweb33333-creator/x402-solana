"""Endpoint — OpenStreetMap building footprint (empreinte bâtiment) via Overpass.

Wrapper de l'API Overpass (OpenStreetMap) : pour un point (lat/lon + rayon) ou une
bbox, renvoie les bâtiments OSM avec leur géométrie en GeoJSON (Polygon /
MultiPolygon), leurs tags utiles (type, niveaux, hauteur, nom), leur centroïde et
une estimation de la surface au sol (m²) calculée côté serveur.

Source : OpenStreetMap via Overpass API (overpass-api.de), sans clé.
Usage responsable : UNE requête à la fois, rayon/bbox TOUJOURS bornés, [timeout]
obligatoire dans la requête Overpass QL ; retry+backoff sur 429/504 (file pleine).

Tier $0.01. TTL 7 jours (empreintes de bâtiments stables).
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

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SOURCE_NAME = "OpenStreetMap via Overpass API (overpass-api.de)"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

_MAX_RADIUS_M = 200          # rayon max borné
_DEFAULT_RADIUS_M = 50
_MAX_BBOX_SPAN_DEG = 0.02    # ~2.2 km : bbox bornée pour rester raisonnable
_OVERPASS_TIMEOUT = 25       # [timeout:25] dans la requête QL

_CACHE_TTL = 7 * 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(40.0, connect=6.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0
_HEADERS = {"User-Agent": "x402-endpoints/1.0 (OSM building footprint)"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _ring_metrics(coords: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Surface (m²) + centroïde (lon,lat) d'un anneau [(lon,lat), ...] fermé.

    Projection équirectangulaire locale autour de la latitude moyenne, puis
    formule du lacet (shoelace). Suffisant pour des empreintes de bâtiment.
    """
    if len(coords) < 4:
        return 0.0, coords[0][0] if coords else 0.0, coords[0][1] if coords else 0.0
    lat0 = sum(p[1] for p in coords) / len(coords)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    xy = [((lon * m_per_deg_lon), (lat * m_per_deg_lat)) for lon, lat in coords]
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(xy) - 1):
        x1, y1 = xy[i]
        x2, y2 = xy[i + 1]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area = abs(area2) / 2.0
    if abs(area2) < 1e-9:
        clon = sum(p[0] for p in coords) / len(coords)
        clat = sum(p[1] for p in coords) / len(coords)
        return 0.0, clon, clat
    cx /= (3.0 * area2)
    cy /= (3.0 * area2)
    return area, cx / m_per_deg_lon, cy / m_per_deg_lat


def _geom_to_ring(geometry: list[dict[str, Any]]) -> list[tuple[float, float]]:
    ring = [(pt["lon"], pt["lat"]) for pt in geometry if "lon" in pt and "lat" in pt]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _useful_tags(tags: dict[str, Any]) -> dict[str, Any]:
    keep = ("building", "building:levels", "height", "name", "addr:housenumber",
            "addr:street", "addr:city", "addr:postcode", "amenity")
    return {k: tags.get(k) for k in keep if tags.get(k) is not None}


def _shape_way(el: dict[str, Any]) -> dict[str, Any] | None:
    ring = _geom_to_ring(el.get("geometry") or [])
    if len(ring) < 4:
        return None
    area, clon, clat = _ring_metrics(ring)
    return {
        "osm_type": "way",
        "osm_id": el.get("id"),
        "tags": _useful_tags(el.get("tags") or {}),
        "centroid": {"lat": round(clat, 7), "lon": round(clon, 7)},
        "footprint_area_m2": round(area, 1),
        "geometry": {"type": "Polygon", "coordinates": [[[round(lon, 7), round(lat, 7)] for lon, lat in ring]]},
    }


def _shape_relation(el: dict[str, Any]) -> dict[str, Any] | None:
    outers: list[list[tuple[float, float]]] = []
    inners: list[list[tuple[float, float]]] = []
    for mem in el.get("members") or []:
        if mem.get("type") != "way" or not mem.get("geometry"):
            continue
        ring = _geom_to_ring(mem["geometry"])
        if len(ring) < 4:
            continue
        (inners if mem.get("role") == "inner" else outers).append(ring)
    if not outers:
        return None
    polygons = []
    total_area = 0.0
    all_pts: list[tuple[float, float]] = []
    for outer in outers:
        rings = [[[round(lon, 7), round(lat, 7)] for lon, lat in outer]]
        oarea, _, _ = _ring_metrics(outer)
        total_area += oarea
        all_pts.extend(outer)
        for inner in inners:
            iarea, _, _ = _ring_metrics(inner)
            total_area -= iarea
            rings.append([[round(lon, 7), round(lat, 7)] for lon, lat in inner])
        polygons.append(rings)
    _, clon, clat = _ring_metrics(all_pts + [all_pts[0]] if all_pts else [])
    return {
        "osm_type": "relation",
        "osm_id": el.get("id"),
        "tags": _useful_tags(el.get("tags") or {}),
        "centroid": {"lat": round(clat, 7), "lon": round(clon, 7)},
        "footprint_area_m2": round(max(total_area, 0.0), 1),
        "geometry": {"type": "MultiPolygon", "coordinates": polygons},
    }


def _build_query(lat: float | None, lon: float | None, radius: int,
                 bbox: tuple[float, float, float, float] | None) -> str:
    head = f"[out:json][timeout:{_OVERPASS_TIMEOUT}];"
    if bbox is not None:
        s, w, n, e = bbox
        flt = f"({s},{w},{n},{e})"
        body = f"(way[building]{flt};relation[building]{flt};);"
    else:
        flt = f"(around:{radius},{lat},{lon})"
        body = f"(way[building]{flt};relation[building]{flt};);"
    return head + body + "out geom;"


async def lookup_buildings(
    lat: float | None, lon: float | None, radius: int | None,
    bbox: str | None,
) -> dict[str, Any]:
    bbox_tuple: tuple[float, float, float, float] | None = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            raise HTTPException(status_code=400, detail="'bbox' attendu: 'south,west,north,east'.")
        try:
            s, w, n, e = (float(p) for p in parts)
        except ValueError:
            raise HTTPException(status_code=400, detail="'bbox' doit contenir 4 nombres 'south,west,north,east'.")
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
            raise HTTPException(status_code=400, detail="'bbox' invalide (ordre south<north, west<east, bornes lat/lon).")
        if (n - s) > _MAX_BBOX_SPAN_DEG or (e - w) > _MAX_BBOX_SPAN_DEG:
            raise HTTPException(
                status_code=400,
                detail=f"'bbox' trop large (max {_MAX_BBOX_SPAN_DEG}° par côté, ~2 km). Restreindre la zone.")
        bbox_tuple = (s, w, n, e)
        rad = _DEFAULT_RADIUS_M
    else:
        if lat is None or lon is None:
            raise HTTPException(status_code=400, detail="Fournir 'lat'+'lon' (point) ou 'bbox' (south,west,north,east).")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=400, detail="'lat'/'lon' hors bornes.")
        rad = _DEFAULT_RADIUS_M if radius is None else int(radius)
        if not (1 <= rad <= _MAX_RADIUS_M):
            raise HTTPException(status_code=400, detail=f"'radius' attendu dans [1, {_MAX_RADIUS_M}] mètres.")

    if bbox_tuple is not None:
        cache_key = f"bbox|{bbox_tuple}"
    else:
        cache_key = f"pt|{round(lat, 6)}|{round(lon, 6)}|{rad}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    query = _build_query(lat, lon, rad, bbox_tuple)
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(OVERPASS_URL, data={"data": query})
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    elements = resp.json().get("elements", [])
                    buildings = []
                    for el in elements:
                        shaped = _shape_way(el) if el.get("type") == "way" else (
                            _shape_relation(el) if el.get("type") == "relation" else None)
                        if shaped is not None:
                            buildings.append(shaped)
                    shaped_resp = {
                        "query": (
                            {"bbox": {"south": bbox_tuple[0], "west": bbox_tuple[1],
                                      "north": bbox_tuple[2], "east": bbox_tuple[3]}}
                            if bbox_tuple is not None
                            else {"lat": lat, "lon": lon, "radius_m": rad}
                        ),
                        "count": len(buildings),
                        "buildings": buildings,
                        "source": SOURCE_NAME,
                        "attribution": OSM_ATTRIBUTION,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    _cache[cache_key] = (time.time(), shaped_resp)
                    return {**shaped_resp, "cached": False}
                if resp.status_code in (429, 504):
                    last_exc = httpx.HTTPStatusError(
                        f"Overpass busy HTTP {resp.status_code}", request=resp.request, response=resp)
                elif resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête Overpass rejetée (paramètres).")
                else:
                    last_exc = httpx.HTTPStatusError(
                        f"Overpass HTTP {resp.status_code}", request=resp.request, response=resp)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Overpass (OSM).")
    if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code in (429, 504):
        raise HTTPException(status_code=503, detail="Overpass (OSM) saturé (file pleine), réessayer plus tard.")
    raise HTTPException(status_code=502, detail="Service Overpass (OSM) indisponible.")


@router.get("/osm/building-footprint")
async def osm_building_footprint(
    lat: float | None = Query(None, description="Latitude of the point, e.g. 48.8584"),
    lon: float | None = Query(None, description="Longitude of the point, e.g. 2.2945"),
    radius: int | None = Query(None, description="Search radius in metres [1-200], default 50"),
    bbox: str | None = Query(None, description="Alternative to lat/lon: 'south,west,north,east' (max ~2km span)"),
) -> JSONResponse:
    """GET /osm/building-footprint — empreintes de bâtiments OSM (géométrie GeoJSON + surface)."""
    data = await lookup_buildings(lat, lon, radius, bbox)
    return JSONResponse(content=data)


@router.get("/osm/health")
async def osm_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers=_HEADERS) as client:
            r = await client.get("https://overpass-api.de/api/status")
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "osm-building-footprint",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
