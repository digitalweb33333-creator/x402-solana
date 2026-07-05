"""Endpoint — Légifrance (droit français, via DILA/PISTE).

Wrapper de l'API officielle Légifrance (DILA) exposée sur le portail PISTE :
recherche dans les textes juridiques français (codes, lois/décrets, JORF) ou
consultation d'un texte/article par identifiant. Complète EUR-Lex (droit UE) côté
droit national français.

Auth : OAuth2 client_credentials sur PISTE (oauth.piste.gouv.fr) ; token mis en
cache et renouvelé automatiquement (client httpx partagé), comme pour EPO.
La plupart des endpoints Légifrance sont des POST avec un corps JSON structuré —
on construit le payload côté serveur à partir de params simples.

Source : Légifrance via DILA/PISTE (api.piste.gouv.fr). Tier $0.05. TTL 24h.
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import LEGIFRANCE_CLIENT_ID, LEGIFRANCE_CLIENT_SECRET

router = APIRouter()

TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
# Base API Légifrance sur PISTE : /dila/legifrance/lf-engine-app (PAS /lp/v1).
# Réf. doc officielle Légifrance (data.gouv.fr / legifrance open-data-et-api).
API_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
SOURCE_NAME = "Légifrance via DILA/PISTE (api.piste.gouv.fr)"
DISCLAIMER = "Données indicatives issues de Légifrance (DILA), pas un avis juridique."

_FONDS = {"LODA_DATE", "CODE_DATE", "JORF", "CODE_ETAT", "LODA_ETAT", "ALL"}

_CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.6
_HEADERS = {"User-Agent": "x402-endpoints/1.0"}

_token: dict[str, Any] = {"value": None, "expiry": 0.0}
_token_lock = asyncio.Lock()


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


async def _get_token(client: httpx.AsyncClient, force: bool = False) -> str:
    async with _token_lock:
        if not force and _token["value"] and time.time() < _token["expiry"]:
            return _token["value"]
        try:
            resp = await client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials",
                      "client_id": LEGIFRANCE_CLIENT_ID,
                      "client_secret": LEGIFRANCE_CLIENT_SECRET,
                      "scope": "openid"},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Échec de connexion au service d'authentification PISTE (Légifrance).")
        if resp.status_code in (400, 401):
            raise HTTPException(
                status_code=502,
                detail="Authentification Légifrance PISTE refusée (invalid_client) — vérifier LEGIFRANCE_CLIENT_ID/SECRET "
                       "ou l'activation de l'accès Légifrance sur l'application PISTE.")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Échec d'obtention du token PISTE (HTTP {resp.status_code}).")
        body = resp.json()
        _token["value"] = body["access_token"]
        _token["expiry"] = time.time() + int(body.get("expires_in", 3600)) - 60
        return _token["value"]


async def _authed_post(client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
    """POST authentifié avec renouvellement transparent du token sur 401."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        token = await _get_token(client, force=(attempt > 0))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Content-Type": "application/json"}
        try:
            resp = await client.post(f"{API_BASE}{path}", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 401 and attempt < _MAX_ATTEMPTS - 1:
                _token["value"] = None  # token expiré -> refresh transparent
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
                continue
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
                continue
            return resp
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Légifrance (PISTE).")
    raise HTTPException(status_code=502, detail="Service Légifrance (PISTE) indisponible.")


def _shape_result(r: dict[str, Any]) -> dict[str, Any]:
    titles = r.get("titles") or []
    first_title = titles[0] if titles and isinstance(titles, list) else {}
    return {
        "id": r.get("id") or first_title.get("id"),
        "cid": r.get("cid") or first_title.get("cid"),
        "title": r.get("title") or r.get("titre") or first_title.get("title"),
        "nature": r.get("nature"),
        "date": r.get("date") or r.get("datePublication"),
        "etat": r.get("etat") or r.get("legalStatus"),
        "text_summary": (r.get("text") or r.get("extract") or None),
    }


async def _search(client: httpx.AsyncClient, q: str, fond: str, limit: int) -> dict[str, Any]:
    payload = {
        "recherche": {
            "champs": [{
                "typeChamp": "ALL",
                "criteres": [{"typeRecherche": "UN_DES_MOTS", "valeur": q, "operateur": "ET"}],
                "operateur": "ET",
            }],
            "filtres": [],
            "pageNumber": 1,
            "pageSize": limit,
            "operateur": "ET",
            "sort": "PERTINENCE",
            "typePagination": "DEFAUT",
        },
        "fond": fond,
    }
    resp = await _authed_post(client, "/search", payload)
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail="Requête de recherche Légifrance invalide.")
    if resp.status_code == 404:
        # Légifrance renvoie 200 + results vides quand il n'y a pas de résultat ; un 404
        # ici = l'API n'est pas routée pour l'app PISTE (abonnement Légifrance manquant/non activé).
        raise HTTPException(
            status_code=502,
            detail="API Légifrance non routée (HTTP 404) — l'application PISTE n'est probablement pas abonnée "
                   "à l'API Légifrance (ou accès DILA non encore activé). Le token OAuth2 est pourtant valide.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse Légifrance inattendue (HTTP {resp.status_code}).")
    body = resp.json()
    results = body.get("results", []) or []
    return {
        "mode": "search", "query": q, "fond": fond,
        "total": body.get("totalResultNumber"),
        "count": len(results),
        "results": [_shape_result(r) for r in results],
    }


async def _consult(client: httpx.AsyncClient, text_id: str) -> dict[str, Any]:
    # Consultation d'un texte JORF par identifiant (POST /consult/jorf).
    resp = await _authed_post(client, "/consult/jorf", {"textCid": text_id})
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Texte Légifrance introuvable pour l'identifiant {text_id}.")
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail="Identifiant de texte Légifrance invalide.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse Légifrance inattendue (HTTP {resp.status_code}).")
    return {"mode": "consult", "text_id": text_id, "document": resp.json()}


async def search_legifrance(q: str | None, fond: str | None, text_id: str | None, limit: int) -> dict[str, Any]:
    text_id = (text_id or "").strip()
    q = (q or "").strip()
    f = (fond or "LODA_DATE").strip().upper()
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")
    if not text_id and not q:
        raise HTTPException(status_code=400, detail="Fournir 'q' (mot-clé de recherche) ou 'text_id' (consultation).")
    if not text_id and f not in _FONDS:
        raise HTTPException(status_code=400, detail=f"'fond' attendu parmi : {', '.join(sorted(_FONDS))}.")

    cache_key = f"consult|{text_id}" if text_id else f"search|{q.lower()}|{f}|{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        if text_id:
            data = await _consult(client, text_id)
        else:
            data = await _search(client, q, f, limit)

    enriched = {**data, "source": SOURCE_NAME,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "disclaimer": DISCLAIMER}
    _cache[cache_key] = (time.time(), enriched)
    return {**enriched, "cached": False}


@router.get("/fr-legifrance/search")
async def fr_legifrance_search(
    q: str | None = Query(None, description="Keyword search in French legal texts, e.g. 'données personnelles'"),
    fond: str | None = Query("LODA_DATE", description="Corpus: LODA_DATE (laws/decrees) | CODE_DATE | JORF (default LODA_DATE)"),
    text_id: str | None = Query(None, description="If set, consult a specific text by id/cid instead of searching"),
    limit: int = Query(10, description="Max results [1-50], e.g. 10"),
) -> JSONResponse:
    """GET /fr-legifrance/search — recherche / consultation de textes juridiques FR (Légifrance / DILA)."""
    data = await search_legifrance(q, fond, text_id, limit)
    return JSONResponse(content=data)


@router.get("/fr-legifrance/health")
async def fr_legifrance_health() -> JSONResponse:
    """Santé Légifrance : token PISTE + ping léger officiel (/list/ping), pas une recherche."""
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers=_HEADERS) as client:
            token = await _get_token(client)
            # Ping officiel léger /list/ping (renvoie "pong" en text/plain ; Accept JSON -> 500).
            r = await client.get(f"{API_BASE}/list/ping",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"})
            upstream_ok = r.status_code == 200
            detail = "token OK + ping OK (pong)" if upstream_ok else f"token OK but API ping HTTP {r.status_code}"
    except HTTPException as exc:
        detail = f"{exc.status_code} {exc.detail}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "fr-legifrance",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(LEGIFRANCE_CLIENT_ID and LEGIFRANCE_CLIENT_SECRET),
            "cache_entries": len(_cache),
        },
    )
