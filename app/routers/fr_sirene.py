"""Endpoint — INSEE Sirene (registre officiel des entreprises FR).

Wrapper de l'API officielle INSEE Sirene V3.11 (nouveau portail api.insee.fr) :
lookup par SIRET (établissement), par SIREN (unité légale) ou recherche
multicritère/texte. Renvoie une synthèse propre de l'établissement / unité légale.

⚠️ AUTH : le nouveau portail n'utilise PLUS OAuth2 — juste une clé API en header
`X-INSEE-Api-Key-Integration` sur chaque appel (pas de token, pas de Bearer).

Source : INSEE Sirene V3.11 (api.insee.fr). Tier $0.01. TTL 6h.
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import INSEE_SIRENE_API_KEY

router = APIRouter()

BASE = "https://api.insee.fr/api-sirene/3.11"
SOURCE_NAME = "INSEE Sirene V3.11 (api.insee.fr)"
DISCLAIMER = "Données indicatives issues du registre INSEE Sirene, pas un avis de conformité."

_CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(12.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.6
_HEADERS = {
    "User-Agent": "x402-endpoints/1.0",
    "Accept": "application/json",
    "X-INSEE-Api-Key-Integration": INSEE_SIRENE_API_KEY,
}


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
                last_exc = httpx.HTTPStatusError("INSEE 429", request=resp.request, response=resp)
            else:
                return resp
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant INSEE Sirene.")
    if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code == 429:
        raise HTTPException(status_code=503, detail="INSEE Sirene rate-limited (429, 30 req/min), réessayer.")
    raise HTTPException(status_code=502, detail="Service INSEE Sirene indisponible.")


def _addresse(adr: dict[str, Any]) -> str | None:
    parts = [
        adr.get("numeroVoieEtablissement"),
        adr.get("typeVoieEtablissement"),
        adr.get("libelleVoieEtablissement"),
        adr.get("codePostalEtablissement"),
        adr.get("libelleCommuneEtablissement"),
    ]
    line = " ".join(str(p) for p in parts if p)
    return line or None


def _shape_etablissement(et: dict[str, Any]) -> dict[str, Any]:
    ul = et.get("uniteLegale", {}) or {}
    periodes = et.get("periodesEtablissement") or [{}]
    p0 = periodes[0] if periodes else {}
    return {
        "siren": et.get("siren"),
        "nic": et.get("nic"),
        "siret": et.get("siret"),
        "denomination": ul.get("denominationUniteLegale")
                        or " ".join(filter(None, [ul.get("prenom1UniteLegale"), ul.get("nomUniteLegale")])) or None,
        "etat_administratif": p0.get("etatAdministratifEtablissement"),
        "etat_unite_legale": ul.get("etatAdministratifUniteLegale"),
        "date_creation": et.get("dateCreationEtablissement"),
        "categorie_juridique": ul.get("categorieJuridiqueUniteLegale"),
        "activite_principale_naf": p0.get("activitePrincipaleEtablissement") or et.get("activitePrincipaleNAF25Etablissement"),
        "tranche_effectifs": et.get("trancheEffectifsEtablissement"),
        "etablissement_siege": et.get("etablissementSiege"),
        "statut_diffusion": et.get("statutDiffusionEtablissement"),
        "adresse": _addresse(et.get("adresseEtablissement", {}) or {}),
    }


def _shape_unite_legale(ul: dict[str, Any]) -> dict[str, Any]:
    periodes = ul.get("periodesUniteLegale") or [{}]
    p0 = periodes[0] if periodes else {}
    return {
        "siren": ul.get("siren"),
        "denomination": p0.get("denominationUniteLegale")
                        or " ".join(filter(None, [p0.get("prenom1UniteLegale"), p0.get("nomUniteLegale")])) or None,
        "etat_administratif": p0.get("etatAdministratifUniteLegale"),
        "categorie_juridique": p0.get("categorieJuridiqueUniteLegale"),
        "activite_principale_naf": p0.get("activitePrincipaleUniteLegale"),
        "date_creation": ul.get("dateCreationUniteLegale"),
        "tranche_effectifs": ul.get("trancheEffectifsUniteLegale"),
        "statut_diffusion": ul.get("statutDiffusionUniteLegale"),
        "categorie_entreprise": ul.get("categorieEntreprise"),
    }


async def lookup(siret: str | None, siren: str | None, q: str | None, limit: int) -> dict[str, Any]:
    siret = (siret or "").strip()
    siren = (siren or "").strip()
    q = (q or "").strip()
    if not (siret or siren or q):
        raise HTTPException(status_code=400, detail="Fournir au moins 'siret' (14 chiffres), 'siren' (9 chiffres) ou 'q' (recherche).")
    if siret and (len(siret) != 14 or not siret.isdigit()):
        raise HTTPException(status_code=400, detail="'siret' doit comporter exactement 14 chiffres.")
    if siren and (len(siren) != 9 or not siren.isdigit()):
        raise HTTPException(status_code=400, detail="'siren' doit comporter exactement 9 chiffres.")
    if not (1 <= limit <= 100):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 100].")

    if siret:
        cache_key = f"siret|{siret}"
    elif siren:
        cache_key = f"siren|{siren}"
    else:
        cache_key = f"q|{q.lower()}|{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        if siret:
            resp = await _request(client, f"{BASE}/siret/{siret}")
            data = _handle_single(resp, "siret", siret, lambda j: {
                "mode": "siret", "etablissement": _shape_etablissement(j.get("etablissement", {}))})
        elif siren:
            resp = await _request(client, f"{BASE}/siren/{siren}")
            data = _handle_single(resp, "siren", siren, lambda j: {
                "mode": "siren", "unite_legale": _shape_unite_legale(j.get("uniteLegale", {}))})
        else:
            resp = await _request(client, f"{BASE}/siret", params={"q": q, "nombre": limit})
            if resp.status_code == 400:
                raise HTTPException(status_code=400, detail="Requête de recherche INSEE invalide (syntaxe 'q').")
            if resp.status_code == 401:
                raise HTTPException(status_code=502, detail="Authentification INSEE refusée (clé API invalide).")
            if resp.status_code == 404:
                data = {"mode": "search", "query": q, "total": 0, "count": 0, "etablissements": []}
            elif resp.status_code == 200:
                body = resp.json()
                etabs = body.get("etablissements", []) or []
                data = {"mode": "search", "query": q,
                        "total": (body.get("header", {}) or {}).get("total"),
                        "count": len(etabs),
                        "etablissements": [_shape_etablissement(e) for e in etabs]}
            else:
                raise HTTPException(status_code=502, detail=f"Réponse INSEE inattendue (HTTP {resp.status_code}).")

    enriched = {**data, "source": SOURCE_NAME,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "disclaimer": DISCLAIMER}
    _cache[cache_key] = (time.time(), enriched)
    return {**enriched, "cached": False}


def _handle_single(resp: httpx.Response, kind: str, value: str, shaper) -> dict[str, Any]:
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Aucun élément trouvé pour le {kind} {value}.")
    if resp.status_code == 401:
        raise HTTPException(status_code=502, detail="Authentification INSEE refusée (clé API invalide).")
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail=f"'{kind}' invalide pour INSEE.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse INSEE inattendue (HTTP {resp.status_code}).")
    return shaper(resp.json())


@router.get("/fr-sirene/lookup")
async def fr_sirene_lookup(
    siret: str | None = Query(None, description="14-digit SIRET (establishment), e.g. '44306184100047'"),
    siren: str | None = Query(None, description="9-digit SIREN (legal unit), e.g. '443061841'"),
    q: str | None = Query(None, description="Free-text / multi-criteria search, e.g. 'denominationUniteLegale:\"GOOGLE FRANCE\"'"),
    limit: int = Query(20, description="Max results for search [1-100], e.g. 20"),
) -> JSONResponse:
    """GET /fr-sirene/lookup — registre officiel INSEE Sirene (établissement / unité légale / recherche)."""
    data = await lookup(siret, siren, q, limit)
    return JSONResponse(content=data)


@router.get("/fr-sirene/health")
async def fr_sirene_health() -> JSONResponse:
    """Santé INSEE : ping léger (1 SIRET connu), pas une recherche lourde."""
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers=_HEADERS) as client:
            r = await client.get(f"{BASE}/siret/44306184100047")
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "fr-sirene",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(INSEE_SIRENE_API_KEY),
            "cache_entries": len(_cache),
        },
    )
