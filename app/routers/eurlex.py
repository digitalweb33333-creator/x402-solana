"""Endpoint — EUR-Lex (législation, traités, jurisprudence UE).

Wrapper du point SPARQL public CELLAR de l'Office des publications de l'UE :
recherche plein-texte (index Virtuoso `bif:contains`) dans les titres des
actes juridiques de l'UE, renvoie le numéro CELEX, le titre et la date.

Source : EUR-Lex / CELLAR SPARQL (publications.europa.eu), public, sans clé.
Tier "verif" $0.01. TTL 6h.
"""

import asyncio
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

SPARQL_URL = "http://publications.europa.eu/webapi/rdf/sparql"
SOURCE_NAME = "EUR-Lex / CELLAR SPARQL (publications.europa.eu)"
_LANGS = {"en": "ENG", "fr": "FRA", "de": "DEU", "es": "SPA", "it": "ITA", "nl": "NLD", "pl": "POL"}

_CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
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


def _build_sparql(text: str, lang3: str, limit: int) -> str:
    # text nettoyé pour bif:contains (mots alphanumériques, quotes simples).
    words = re.findall(r"[A-Za-z0-9À-ÿ]+", text)[:8]
    bif = " and ".join(f"'{w}'" for w in words)
    return f'''PREFIX cdm:<http://publications.europa.eu/ontology/cdm#>
SELECT ?celex ?title ?date WHERE {{
  ?exp cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/{lang3}> ;
       cdm:expression_title ?title ;
       cdm:expression_belongs_to_work ?w .
  ?title bif:contains "{bif}" .
  ?w cdm:resource_legal_id_celex ?celex ;
     cdm:work_date_document ?date .
}} ORDER BY DESC(?date) LIMIT {limit}'''


async def search_eurlex(query: str | None, language: str | None, limit: int) -> dict[str, Any]:
    text = (query or "").strip()
    if len(text) < 3 or not re.search(r"[A-Za-z0-9]", text):
        raise HTTPException(status_code=400, detail="Paramètre 'query' requis (≥ 3 caractères alphanumériques).")
    lang = (language or "en").strip().lower()
    if lang not in _LANGS:
        raise HTTPException(status_code=400, detail=f"'language' attendu parmi : {', '.join(_LANGS)}.")
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")

    key = f"{text.lower()}|{lang}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    sparql = _build_sparql(text, _LANGS[lang], limit)
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(SPARQL_URL, params={"query": sparql, "format": "application/sparql-results+json"})
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    bindings = resp.json().get("results", {}).get("bindings", [])
                    seen, results = set(), []
                    for b in bindings:
                        celex = b.get("celex", {}).get("value")
                        if celex in seen:
                            continue
                        seen.add(celex)
                        results.append({
                            "celex": celex,
                            "title": b.get("title", {}).get("value"),
                            "date": b.get("date", {}).get("value"),
                            "url": f"https://eur-lex.europa.eu/legal-content/{lang.upper()}/TXT/?uri=CELEX:{celex}",
                        })
                    shaped = {
                        "query": text,
                        "language": lang,
                        "count": len(results),
                        "documents": results,
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    _cache[key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                last_exc = httpx.HTTPStatusError(
                    f"EUR-Lex HTTP {resp.status_code}", request=resp.request, response=resp
                )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant EUR-Lex (CELLAR).")
    raise HTTPException(status_code=502, detail="Service EUR-Lex (CELLAR SPARQL) indisponible.")


@router.get("/eurlex/search")
async def eurlex_search(
    query: str = Query(..., description="Keywords searched in act titles, e.g. 'data protection'"),
    language: str = Query("en", description="Language: en, fr, de, es, it, nl, pl (default en)"),
    limit: int = Query(10, description="Max documents to return [1-50], e.g. 10"),
) -> JSONResponse:
    """GET /eurlex/search?query=&language=&limit= — recherche législation UE (EUR-Lex)."""
    data = await search_eurlex(query, language, limit)
    return JSONResponse(content=data)


@router.get("/eurlex/health")
async def eurlex_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            r = await client.get(SPARQL_URL, params={"query": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1", "format": "application/sparql-results+json"})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "eurlex",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
