"""Endpoint #1 (référence) — GLEIF LEI lookup.

Wrapper de l'API officielle GLEIF (https://api.gleif.org) : prend un LEI
(Legal Entity Identifier, code ISO 17442 à 20 caractères) et renvoie l'entité
légale correspondante (nom, statut, juridiction, forme juridique, adresse,
date d'enregistrement, autorité d'enregistrement).

Source : GLEIF API v1, publique, gratuite, sans clé. JSON:API.
Protégé par le middleware x402 (cf app/main.py) — tier "verif" $0.01.

Robustesse (cf CLAUDE.md) :
- cache mémoire TTL 24h (un LEI change rarement),
- retry + backoff sur l'appel source, timeout court,
- codes HTTP propres (400 format, 404 introuvable, 502 source down, 504 timeout),
- jamais de 500 nu,
- /gleif/health expose la dispo de la source upstream.

NB architecture x402 : la passerelle de paiement s'exécute AVANT ce handler.
Un appel non payé est donc intercepté en 402 par le middleware et n'atteint
jamais la logique 400/404 ci-dessous (qui ne se manifeste que sur appel payé).
La logique métier est exposée via `lookup_lei()` pour pouvoir être testée
directement, indépendamment du paywall.
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

# --- Constantes source ---
GLEIF_BASE = "https://api.gleif.org/api/v1/lei-records"
GLEIF_HEADERS = {"Accept": "application/vnd.api+json"}
SOURCE_NAME = "GLEIF API v1 (gleif.org)"
DISCLAIMER = "Données indicatives issues du registre GLEIF, pas un avis de conformité."

# Format LEI : 20 caractères alphanumériques majuscules (ISO 17442).
LEI_RE = re.compile(r"^[A-Z0-9]{20}$")

# --- Cache mémoire (TTL 24h) ---
_CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# --- Réglages réseau source ---
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # 0.5s, 1s, 2s


def _cache_get(lei: str) -> dict[str, Any] | None:
    hit = _cache.get(lei)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(lei, None)
        return None
    return data


def _shape(record: dict[str, Any], lei: str) -> dict[str, Any]:
    """Normalise un enregistrement GLEIF JSON:API en réponse plate et stable."""
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {}) or {}
    registration = attrs.get("registration", {}) or {}
    addr = entity.get("legalAddress", {}) or {}
    legal_form = entity.get("legalForm", {}) or {}

    return {
        "lei": attrs.get("lei", lei),
        "legal_name": (entity.get("legalName") or {}).get("name"),
        "entity_status": entity.get("status"),  # ACTIVE / INACTIVE
        "registration_status": registration.get("status"),  # ISSUED / LAPSED / ...
        "jurisdiction": entity.get("jurisdiction"),
        "legal_form_code": legal_form.get("id"),
        "legal_address": {
            "lines": addr.get("addressLines"),
            "city": addr.get("city"),
            "region": addr.get("region"),
            "country": addr.get("country"),
            "postal_code": addr.get("postalCode"),
        },
        "initial_registration_date": registration.get("initialRegistrationDate"),
        "last_update_date": registration.get("lastUpdateDate"),
        "next_renewal_date": registration.get("nextRenewalDate"),
        "managing_lou": registration.get("managingLou"),
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }


async def _fetch_gleif(lei: str) -> dict[str, Any]:
    """Appelle GLEIF avec retry/backoff. Lève HTTPException sur erreur propre."""
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=GLEIF_HEADERS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(f"{GLEIF_BASE}/{lei}")
            except (httpx.TimeoutException,) as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json().get("data", {})
                if resp.status_code == 404:
                    raise HTTPException(
                        status_code=404,
                        detail="LEI bien formé mais introuvable dans le registre GLEIF.",
                    )
                # 5xx / 429 / autres → réessayable
                last_exc = httpx.HTTPStatusError(
                    f"GLEIF HTTP {resp.status_code}", request=resp.request, response=resp
                )

            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    # Épuisé : distinguer timeout (504) de source down (502).
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant GLEIF.")
    raise HTTPException(status_code=502, detail="Source GLEIF indisponible.")


async def lookup_lei(lei: str | None) -> dict[str, Any]:
    """Logique métier : valide le LEI, sert le cache, sinon interroge GLEIF.

    Lève HTTPException(400) si le format est invalide, (404) si inconnu,
    (502/504) si la source échoue.
    """
    raw = (lei or "").strip().upper()
    if not LEI_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail="LEI invalide : 20 caractères alphanumériques attendus (ISO 17442).",
        )

    cached = _cache_get(raw)
    if cached is not None:
        return {**cached, "cached": True}

    record = await _fetch_gleif(raw)
    shaped = _shape(record, raw)
    _cache[raw] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/gleif/lei")
async def gleif_lei(
    lei: str = Query(
        ...,
        description="20-character LEI, e.g. '529900T8BM49AURSDO55'",
        examples=["529900T8BM49AURSDO55"],
    ),
) -> JSONResponse:
    """GET /gleif/lei?lei=... — entité légale derrière un LEI (registre GLEIF)."""
    data = await lookup_lei(lei)
    return JSONResponse(content=data)


@router.get("/gleif/health")
async def gleif_health() -> JSONResponse:
    """Santé de l'endpoint + disponibilité de la source GLEIF upstream."""
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            r = await client.get(
                f"{GLEIF_BASE}/529900T8BM49AURSDO55", headers=GLEIF_HEADERS
            )
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__

    status = "ok" if upstream_ok else "degraded"
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "gleif",
            "status": status,
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
