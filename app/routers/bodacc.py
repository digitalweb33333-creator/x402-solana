"""Endpoint — BODACC (annonces légales d'entreprises, France).

Wrapper de l'API officielle DILA (BODACC) exposée via OpenDataSoft : recherche
les annonces légales publiées au Bulletin officiel des annonces civiles et
commerciales (créations, modifications, radiations, procédures collectives).

Source : DILA via OpenDataSoft (bodacc-datadila.opendatasoft.com), publique, sans clé.
Tier "verif" $0.01. TTL 1h.
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

ODS_URL = ("https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/"
           "datasets/annonces-commerciales/records")
SOURCE_NAME = "BODACC — DILA via OpenDataSoft (bodacc-datadila.opendatasoft.com)"
DISCLAIMER = "Données indicatives issues du BODACC (DILA), pas un avis juridique."

# familleavis -> libellé attendu côté BODACC (filtre optionnel).
_FAMILLE_MAP = {
    "creation": "Avis de création d'établissement",
    "modification": "Avis de modification",
    "radiation": "Avis de radiation",
    "procedure": "Avis de dépôt des comptes des sociétés",
}
# On filtre plutôt par le champ `familleavis` (code) : creation/modification/radiation/depot/collective.
_FAMILLE_CODES = {"creation", "modification", "radiation", "depot", "collective"}

_CACHE_TTL = 3600
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


def _shape(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec.get("id"),
        "date_parution": rec.get("dateparution"),
        "type": rec.get("typeavis_lib") or rec.get("typeavis"),
        "famille": rec.get("familleavis_lib") or rec.get("familleavis"),
        "registre": rec.get("registre"),
        "commercant": rec.get("commercant"),
        "ville": rec.get("ville"),
        "departement": rec.get("departement_nom_officiel"),
        "tribunal": rec.get("tribunal"),
        "jugement": rec.get("jugement"),
        "url": rec.get("url_complete"),
    }


async def _fetch(params: dict[str, Any]) -> dict[str, Any]:
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(ODS_URL, params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête BODACC invalide (paramètre mal formé).")
                last_exc = httpx.HTTPStatusError(
                    f"BODACC HTTP {resp.status_code}", request=resp.request, response=resp
                )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant BODACC.")
    raise HTTPException(status_code=502, detail="Source BODACC indisponible.")


async def search_annonces(q: str | None, famille: str | None, limit: int) -> dict[str, Any]:
    term = (q or "").strip()
    if len(term) < 2:
        raise HTTPException(status_code=400, detail="Paramètre 'q' requis (≥ 2 caractères : nom, SIREN ou RCS).")
    if not (1 <= limit <= 100):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 100].")
    fam = (famille or "").strip().lower() or None
    if fam and fam not in _FAMILLE_CODES:
        raise HTTPException(
            status_code=400,
            detail="'famille' attendu parmi : creation, modification, radiation, depot, collective.",
        )

    key = f"{term}|{fam}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    # ODSQL : recherche plein-texte + filtre famille optionnel.
    safe = term.replace('"', " ")
    where = f'"{safe}"'
    if fam:
        where += f' and familleavis = "{fam}"'
    payload = await _fetch({"where": where, "limit": limit, "order_by": "dateparution desc"})

    results = payload.get("results", [])
    shaped = {
        "query": term,
        "famille": fam,
        "total_count": payload.get("total_count"),
        "count": len(results),
        "annonces": [_shape(r) for r in results],
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/bodacc/annonces")
async def bodacc_annonces(
    q: str = Query(..., description="Search term: company name, SIREN or RCS, e.g. 'OVH' or '424761419'"),
    famille: str | None = Query(None, description="Optional filter: creation | modification | radiation | depot | collective"),
    limit: int = Query(10, description="Max announcements to return [1-100], e.g. 10"),
) -> JSONResponse:
    """GET /bodacc/annonces?q=&famille=&limit= — annonces légales BODACC (DILA)."""
    data = await search_annonces(q, famille, limit)
    return JSONResponse(content=data)


@router.get("/bodacc/health")
async def bodacc_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            r = await client.get(ODS_URL, params={"limit": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "bodacc",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
