"""Endpoint — Product recalls (rappels produits) via openFDA.

Wrapper de l'API officielle openFDA (FDA Recall Enterprise System) : recherche
les rappels de médicaments (et, en option, dispositifs/aliments) par produit,
firme ou raison, avec classification I/II/III.

Source : openFDA enforcement (api.fda.gov), publique, sans clé.
NOTE : sans clé openFDA = 240 req/min, 1000 req/jour par IP. Une clé gratuite
openFDA relève ces limites (à prendre si le volume augmente).
Tier "verif" $0.01. TTL 6h (données hebdomadaires).
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import OPENFDA_API_KEY

router = APIRouter()

_ENDPOINTS = {
    "drug": "https://api.fda.gov/drug/enforcement.json",
    "device": "https://api.fda.gov/device/enforcement.json",
    "food": "https://api.fda.gov/food/enforcement.json",
}
SOURCE_NAME = "openFDA — FDA enforcement reports (api.fda.gov)"
DISCLAIMER = "Données indicatives issues d'openFDA (FDA), pas un avis médical ni réglementaire."
_CLASSES = {"I": "Class I", "II": "Class II", "III": "Class III"}

_CACHE_TTL = 6 * 3600
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


def _shape(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "recall_number": r.get("recall_number"),
        "classification": r.get("classification"),
        "status": r.get("status"),
        "product_type": r.get("product_type"),
        "product_description": (r.get("product_description") or "")[:500] or None,
        "reason_for_recall": (r.get("reason_for_recall") or "")[:500] or None,
        "recalling_firm": r.get("recalling_firm"),
        "distribution_pattern": (r.get("distribution_pattern") or "")[:300] or None,
        "recall_initiation_date": r.get("recall_initiation_date"),
        "report_date": r.get("report_date"),
        "voluntary_mandated": r.get("voluntary_mandated"),
        "city": r.get("city"),
        "state": r.get("state"),
        "country": r.get("country"),
    }


async def search_recalls(query: str | None, category: str, classification: str | None, limit: int) -> dict[str, Any]:
    cat = (category or "drug").strip().lower()
    if cat not in _ENDPOINTS:
        raise HTTPException(status_code=400, detail="'category' attendu : drug, device ou food.")
    term = (query or "").strip()
    if len(term) < 2:
        raise HTTPException(status_code=400, detail="Paramètre 'query' requis (≥ 2 caractères : produit, firme ou raison).")
    cls = (classification or "").strip().upper() or None
    if cls and cls not in _CLASSES:
        raise HTTPException(status_code=400, detail="'classification' attendu : I, II ou III.")
    if not (1 <= limit <= 1000):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 1000].")

    key = f"{cat}|{term}|{cls}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    safe = term.replace('"', " ")
    search = f'"{safe}"'
    if cls:
        search += f' AND classification:"{_CLASSES[cls]}"'
    params: dict[str, Any] = {"search": search, "limit": limit}  # limit explicite (openFDA renvoie 1 par défaut)
    if OPENFDA_API_KEY:
        params["api_key"] = OPENFDA_API_KEY

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(_ENDPOINTS[cat], params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    results = payload.get("results", [])
                    shaped = {
                        "query": term, "category": cat, "classification": cls,
                        "total": payload.get("meta", {}).get("results", {}).get("total", 0),
                        "count": len(results),
                        "recalls": [_shape(x) for x in results],
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "disclaimer": DISCLAIMER,
                    }
                    _cache[key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                if resp.status_code == 404:
                    # openFDA renvoie 404 quand aucun match -> réponse vide propre (200).
                    shaped = {
                        "query": term, "category": cat, "classification": cls,
                        "total": 0, "count": 0, "recalls": [],
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "disclaimer": DISCLAIMER,
                    }
                    return {**shaped, "cached": False}
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête openFDA invalide.")
                last_exc = httpx.HTTPStatusError(
                    f"openFDA HTTP {resp.status_code}", request=resp.request, response=resp)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant openFDA.")
    raise HTTPException(status_code=502, detail="Service openFDA indisponible.")


@router.get("/recalls/search")
async def recalls_search(
    query: str = Query(..., description="Product, firm or reason, e.g. 'metformin' or 'contamination'"),
    category: str = Query("drug", description="drug | device | food (default drug)"),
    classification: str | None = Query(None, description="Recall class: I, II or III (optional)"),
    limit: int = Query(20, description="Max recalls to return [1-1000], e.g. 20"),
) -> JSONResponse:
    """GET /recalls/search?query=&category=&classification=&limit= — rappels produits (openFDA)."""
    data = await search_recalls(query, category, classification, limit)
    return JSONResponse(content=data)


@router.get("/recalls/health")
async def recalls_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            hp = {"limit": 1}
            if OPENFDA_API_KEY:
                hp["api_key"] = OPENFDA_API_KEY
            r = await client.get(_ENDPOINTS["drug"], params=hp)
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "recalls",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(OPENFDA_API_KEY),
            "cache_entries": len(_cache),
        },
    )
