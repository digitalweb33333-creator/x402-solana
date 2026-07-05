"""Endpoint — UK Companies House (registre officiel des entreprises UK).

Wrapper de l'API publique officielle UK Companies House : recherche d'entreprises
par nom/numéro, ou profil détaillé par numéro de société. Auth = HTTP Basic, la
clé API en username et mot de passe VIDE.

Source : UK Companies House Public Data API (api.company-information.service.gov.uk).
Tier $0.01 (lookup registre officiel). TTL 6h (statut entreprise évolue lentement).
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import COMPANIES_HOUSE_API_KEY

router = APIRouter()

BASE = "https://api.company-information.service.gov.uk"
SOURCE_NAME = "UK Companies House Public Data API (api.company-information.service.gov.uk)"
DISCLAIMER = "Données indicatives issues du registre UK Companies House, pas un avis de conformité."

_CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(12.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_HEADERS = {"User-Agent": "x402-endpoints/1.0", "Accept": "application/json"}
# Auth Basic : clé en username, mot de passe VIDE (piège classique si oublié).
_AUTH = (COMPANIES_HOUSE_API_KEY, "")


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


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
                last_exc = httpx.HTTPStatusError("CH 429", request=resp.request, response=resp)
            else:
                return resp
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Companies House.")
    if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code == 429:
        raise HTTPException(status_code=503, detail="Companies House rate-limited (429), réessayer.")
    raise HTTPException(status_code=502, detail="Service Companies House indisponible.")


def _shape_hit(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "company_number": item.get("company_number"),
        "company_status": item.get("company_status"),
        "company_type": item.get("company_type"),
        "date_of_creation": item.get("date_of_creation"),
        "address_snippet": item.get("address_snippet"),
    }


def _shape_officer(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "name": item.get("name"),
        "officer_role": item.get("officer_role"),
        "appointed_on": item.get("appointed_on"),
    }
    # identity_verification_status : post-ECCTA — ne le renvoyer QUE s'il existe.
    if "identity_verification_status" in item:
        out["identity_verification_status"] = item.get("identity_verification_status")
    return out


async def _fetch_profile(client: httpx.AsyncClient, number: str) -> dict[str, Any]:
    resp = await _request(client, f"{BASE}/company/{number}")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Société UK introuvable pour le numéro {number}.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse Companies House inattendue (HTTP {resp.status_code}).")
    p = resp.json()

    officers: list[dict[str, Any]] = []
    try:
        oresp = await _request(client, f"{BASE}/company/{number}/officers", params={"items_per_page": 35})
        if oresp.status_code == 200:
            officers = [_shape_officer(o) for o in oresp.json().get("items", [])]
    except HTTPException:
        officers = []  # les officiers sont un bonus : ne pas faire échouer le profil

    return {
        "mode": "profile",
        "company": {
            "company_name": p.get("company_name"),
            "company_number": p.get("company_number"),
            "company_status": p.get("company_status"),
            "company_type": p.get("type"),
            "date_of_creation": p.get("date_of_creation"),
            "jurisdiction": p.get("jurisdiction"),
            "registered_office_address": p.get("registered_office_address"),
            "sic_codes": p.get("sic_codes"),
            "has_insolvency_history": p.get("has_insolvency_history"),
            "has_charges": p.get("has_charges"),
            "previous_company_names": p.get("previous_company_names"),
            "officers": officers,
        },
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }


async def search_companies(q: str | None, company_number: str | None, limit: int, start_index: int) -> dict[str, Any]:
    cnum = (company_number or "").strip()
    if cnum:
        if not cnum.isalnum() or len(cnum) > 12:
            raise HTTPException(status_code=400, detail="'company_number' invalide (alphanumérique, ≤ 12 caractères).")
        cache_key = f"profile|{cnum.upper()}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, auth=_AUTH) as client:
            data = await _fetch_profile(client, cnum.upper())
        _cache[cache_key] = (time.time(), data)
        return {**data, "cached": False}

    term = (q or "").strip()
    if len(term) < 2:
        raise HTTPException(status_code=400, detail="Paramètre 'q' requis (≥ 2 caractères : nom ou numéro de société).")
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")
    if start_index < 0:
        raise HTTPException(status_code=400, detail="'start_index' doit être ≥ 0.")

    cache_key = f"search|{term.lower()}|{limit}|{start_index}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, auth=_AUTH) as client:
        resp = await _request(client, f"{BASE}/search/companies",
                              params={"q": term, "items_per_page": limit, "start_index": start_index})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse Companies House inattendue (HTTP {resp.status_code}).")
    body = resp.json()
    items = body.get("items", []) or []
    shaped = {
        "mode": "search",
        "query": term,
        "total_results": body.get("total_results"),
        "start_index": body.get("start_index", start_index),
        "items_per_page": body.get("items_per_page", limit),
        "count": len(items),
        "companies": [_shape_hit(it) for it in items],
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }
    _cache[cache_key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/uk-companies/search")
async def uk_companies_search(
    q: str | None = Query(None, description="Company name or number, e.g. 'Tesco' or '00445790'"),
    company_number: str | None = Query(None, description="If set, return the detailed profile for this number instead of a search"),
    limit: int = Query(20, description="Max results [1-50], e.g. 20"),
    start_index: int = Query(0, description="Pagination offset, e.g. 0"),
) -> JSONResponse:
    """GET /uk-companies/search — recherche / profil au registre officiel UK Companies House."""
    data = await search_companies(q, company_number, limit, start_index)
    return JSONResponse(content=data)


@router.get("/uk-companies/health")
async def uk_companies_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers=_HEADERS, auth=_AUTH) as client:
            r = await client.get(f"{BASE}/search/companies", params={"q": "tesco", "items_per_page": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "uk-companies",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(COMPANIES_HOUSE_API_KEY),
            "cache_entries": len(_cache),
        },
    )
