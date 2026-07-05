"""Endpoint — Drug labels / interactions (notices médicaments) via openFDA.

Wrapper de l'API officielle openFDA drug/label : pour un médicament (nom generic
ou brand), renvoie les sections clés de la notice FDA — interactions
médicamenteuses, avertissements, contre-indications et indications.

Source : openFDA drug labels (api.fda.gov), publique, sans clé.
Tier "verdict" $0.05. TTL 24h (labels stables).
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

LABEL_URL = "https://api.fda.gov/drug/label.json"
SOURCE_NAME = "openFDA — FDA drug labels (api.fda.gov)"
DISCLAIMER = "Données indicatives issues des notices FDA (openFDA), pas un avis médical."

_CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(10.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5

_MAXLEN = 4000  # tronque les sections (le texte FDA peut être très long)


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _section(res: dict[str, Any], field: str) -> str | None:
    val = res.get(field)
    if not val:
        return None
    text = " ".join(val) if isinstance(val, list) else str(val)
    text = text.strip()
    return (text[:_MAXLEN] + " …[truncated]") if len(text) > _MAXLEN else text or None


def _shape(res: dict[str, Any]) -> dict[str, Any]:
    of = res.get("openfda", {}) or {}
    return {
        "brand_name": of.get("brand_name"),
        "generic_name": of.get("generic_name"),
        "manufacturer_name": of.get("manufacturer_name"),
        "drug_interactions": _section(res, "drug_interactions"),
        "warnings": _section(res, "warnings") or _section(res, "warnings_and_cautions"),
        "contraindications": _section(res, "contraindications"),
        "indications_and_usage": _section(res, "indications_and_usage"),
        "boxed_warning": _section(res, "boxed_warning"),
    }


async def lookup_label(drug: str | None, limit: int) -> dict[str, Any]:
    name = (drug or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Paramètre 'drug' requis (≥ 2 caractères : nom generic ou brand).")
    if not (1 <= limit <= 10):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 10].")

    key = f"{name.lower()}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    safe = name.replace('"', " ")
    search = f'(openfda.generic_name:"{safe}" OR openfda.brand_name:"{safe}")'
    params: dict[str, Any] = {"search": search, "limit": limit}
    if OPENFDA_API_KEY:
        params["api_key"] = OPENFDA_API_KEY

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(LABEL_URL, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    shaped = {
                        "drug": name, "count": len(results),
                        "labels": [_shape(r) for r in results],
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "disclaimer": DISCLAIMER,
                    }
                    _cache[key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                if resp.status_code == 404:
                    raise HTTPException(status_code=404, detail="Aucune notice FDA trouvée pour ce médicament.")
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête openFDA invalide.")
                last_exc = httpx.HTTPStatusError(
                    f"openFDA HTTP {resp.status_code}", request=resp.request, response=resp)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant openFDA.")
    raise HTTPException(status_code=502, detail="Service openFDA indisponible.")


@router.get("/drug/label")
async def drug_label(
    drug: str = Query(..., description="Drug generic or brand name, e.g. 'ibuprofen' or 'Advil'"),
    limit: int = Query(1, description="Max labels to return [1-10], e.g. 1"),
) -> JSONResponse:
    """GET /drug/label?drug=&limit= — interactions, avertissements et contre-indications (openFDA)."""
    data = await lookup_label(drug, limit)
    return JSONResponse(content=data)


@router.get("/drug/health")
async def drug_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            hp = {"limit": 1}
            if OPENFDA_API_KEY:
                hp["api_key"] = OPENFDA_API_KEY
            r = await client.get(LABEL_URL, params=hp)
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "drug-label",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(OPENFDA_API_KEY),
            "cache_entries": len(_cache),
        },
    )
