"""Endpoint — TED (Tenders Electronic Daily, marchés publics UE).

Wrapper de l'API officielle TED de l'Office des publications de l'UE : recherche
les avis de marchés publics dans les 27+ États membres par mots-clés, pays et
code CPV.

Source : TED v3 Search API (api.ted.europa.eu), publique, sans clé.
Tier "verdict" $0.05 (donnée à valeur business). TTL 1h.
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

TED_SEARCH = "https://api.ted.europa.eu/v3/notices/search"
SOURCE_NAME = "EU TED — Tenders Electronic Daily (api.ted.europa.eu)"
_FIELDS = ["publication-number", "notice-title", "buyer-name", "buyer-country",
           "notice-type", "publication-date", "links"]

_CACHE_TTL = 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(15.0, connect=4.0)
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


def _pick_lang(value: Any) -> Any:
    """notice-title / buyer-name sont multilingues {lang: str|list}. Préférer l'anglais."""
    if isinstance(value, dict):
        chosen = value.get("eng") or next(iter(value.values()), None)
        if isinstance(chosen, list):
            return chosen[0] if chosen else None
        return chosen
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _shape(n: dict[str, Any]) -> dict[str, Any]:
    links = n.get("links") or {}
    pdf = (links.get("pdf") or {}).get("ENG") if isinstance(links.get("pdf"), dict) else None
    return {
        "publication_number": n.get("publication-number"),
        "title": _pick_lang(n.get("notice-title")),
        "buyer_name": _pick_lang(n.get("buyer-name")),
        "buyer_country": _pick_lang(n.get("buyer-country")),
        "notice_type": _pick_lang(n.get("notice-type")),
        "publication_date": n.get("publication-date"),
        "url": pdf or f"https://ted.europa.eu/en/notice/{n.get('publication-number')}",
    }


def _build_query(query: str | None, country: str | None, cpv: str | None) -> str:
    parts: list[str] = []
    if cpv:
        parts.append(f"classification-cpv={cpv}*")
    if country:
        parts.append(f"buyer-country={country}")
    if query:
        safe = query.replace('"', " ").replace("(", " ").replace(")", " ").strip()
        parts.append(f'FT~("{safe}")')
    return " AND ".join(parts)


async def search_tenders(query: str | None, country: str | None, cpv: str | None, limit: int) -> dict[str, Any]:
    q = (query or "").strip() or None
    cc = (country or "").strip().upper() or None
    cp = (cpv or "").strip() or None
    if cc and (len(cc) != 3 or not cc.isalpha()):
        raise HTTPException(status_code=400, detail="'country' attendu en code ISO 3 lettres (ex. FRA, DEU).")
    if cp and not cp.isdigit():
        raise HTTPException(status_code=400, detail="'cpv' attendu numérique (ex. 72000000).")
    if not (q or cc or cp):
        raise HTTPException(status_code=400, detail="Au moins un critère requis : query, country ou cpv.")
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")

    expert = _build_query(q, cc, cp)
    key = f"{expert}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    body = {"query": expert, "fields": _FIELDS, "limit": limit, "page": 1}
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(TED_SEARCH, json=body)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    notices = payload.get("notices") or []
                    shaped = {
                        "query": {"text": q, "country": cc, "cpv": cp},
                        "total": payload.get("totalNoticeCount") or payload.get("total"),
                        "count": len(notices),
                        "tenders": [_shape(x) for x in notices],
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    _cache[key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête TED refusée (critères invalides).")
                last_exc = httpx.HTTPStatusError(
                    f"TED HTTP {resp.status_code}", request=resp.request, response=resp
                )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant TED.")
    raise HTTPException(status_code=502, detail="Service TED indisponible.")


@router.get("/ted/tenders")
async def ted_tenders(
    query: str | None = Query(None, description="Keywords, e.g. 'cloud software'"),
    country: str | None = Query(None, description="Buyer country ISO-3 code, e.g. 'FRA', 'DEU'"),
    cpv: str | None = Query(None, description="CPV code prefix, e.g. '72000000' (IT services)"),
    limit: int = Query(10, description="Max notices to return [1-50], e.g. 10"),
) -> JSONResponse:
    """GET /ted/tenders?query=&country=&cpv=&limit= — avis de marchés publics UE (TED)."""
    data = await search_tenders(query, country, cpv, limit)
    return JSONResponse(content=data)


@router.get("/ted/health")
async def ted_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            r = await client.post(TED_SEARCH, json={"query": "buyer-country=FRA", "fields": ["publication-number"], "limit": 1, "page": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "ted",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
