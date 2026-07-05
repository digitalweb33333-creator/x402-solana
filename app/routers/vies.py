"""Endpoint — VIES (validation TVA intracommunautaire UE).

Wrapper de l'API REST officielle de la Commission européenne (VIES) : valide un
numéro de TVA intracommunautaire dans les 27 États membres et renvoie, si valide,
le nom et l'adresse de l'entité enregistrée.

Source : VIES REST API (ec.europa.eu), publique, sans clé.
Tier "verif" $0.01. TTL court (1h) — la validité peut changer.

⚠️ CRITIQUE — VIES a TROIS états, pas deux :
  - valid   : isValid=true  (HTTP 200 légitime)
  - invalid : isValid=false, userError="INVALID" (HTTP 200 légitime, numéro inexistant)
  - unavailable : la base TVA nationale est hors-ligne (userError MS_UNAVAILABLE,
                  SERVICE_UNAVAILABLE, TIMEOUT, *MAX_CONCURRENT_REQ…). Ce n'est PAS
                  un "invalid" : on le mappe en 503 (source down). Confondre les deux
                  donnerait un faux négatif de conformité — interdit.
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

VIES_BASE = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{vat}"
SOURCE_NAME = "EU VIES REST API (ec.europa.eu)"
DISCLAIMER = "Données indicatives issues du registre VIES (Commission UE), pas un avis de conformité."

# 27 États membres au sens VIES : EL = Grèce (pas GR), XI = Irlande du Nord.
VIES_COUNTRIES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",
}

# userError signalant une indisponibilité de la base nationale (≠ INVALID).
_UNAVAILABLE_ERRORS = {
    "MS_UNAVAILABLE", "SERVICE_UNAVAILABLE", "TIMEOUT",
    "MS_MAX_CONCURRENT_REQ", "GLOBAL_MAX_CONCURRENT_REQ",
}

_VAT_RE = re.compile(r"^[A-Z0-9]{2,14}$")

_CACHE_TTL = 3600  # 1h
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
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


def map_vies_payload(payload: dict[str, Any], country: str, vat: str) -> dict[str, Any]:
    """Mappe la réponse VIES vers une sortie plate. Lève HTTPException sur unavailable.

    Exposé pour test direct (simulation des 3 états).
    """
    user_error = (payload.get("userError") or "").upper()

    if user_error in _UNAVAILABLE_ERRORS:
        # Base nationale hors-ligne : surtout PAS "invalid".
        raise HTTPException(
            status_code=503,
            detail=f"Base TVA nationale '{country}' indisponible côté VIES (userError={user_error}).",
        )
    if user_error == "INVALID_INPUT":
        raise HTTPException(status_code=400, detail="Numéro/pays refusé par VIES (INVALID_INPUT).")

    is_valid = payload.get("isValid")
    if is_valid is None:
        # État indéterminé -> traiter comme source en erreur, pas comme invalid.
        raise HTTPException(status_code=502, detail=f"Réponse VIES inexploitable (userError={user_error}).")

    def _clean(v: Any) -> Any:
        return None if v in ("---", "") else v

    return {
        "country_code": country,
        "vat_number": payload.get("vatNumber") or vat,
        "valid": bool(is_valid),  # true = numéro existe, false = numéro inexistant (légitime)
        "name": _clean(payload.get("name")),
        "address": _clean(payload.get("address")),
        "request_date": payload.get("requestDate"),
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }


async def _fetch_vies(country: str, vat: str) -> dict[str, Any]:
    last_exc: Exception | None = None
    url = VIES_BASE.format(country=country, vat=vat)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(url)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête VIES invalide (pays/numéro mal formé).")
                last_exc = httpx.HTTPStatusError(
                    f"VIES HTTP {resp.status_code}", request=resp.request, response=resp
                )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant VIES.")
    raise HTTPException(status_code=502, detail="Service VIES indisponible.")


async def lookup_vat(country: str | None, vat: str | None) -> dict[str, Any]:
    cc = (country or "").strip().upper()
    num = re.sub(r"\s+", "", (vat or "")).upper()

    if cc not in VIES_COUNTRIES:
        raise HTTPException(
            status_code=400,
            detail="Code pays invalide : un des 27 États membres VIES attendu (EL pour la Grèce).",
        )
    if not _VAT_RE.match(num):
        raise HTTPException(status_code=400, detail="Numéro de TVA mal formé (2 à 14 caractères alphanumériques).")

    key = f"{cc}:{num}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    payload = await _fetch_vies(cc, num)
    shaped = map_vies_payload(payload, cc, num)
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/vies/vat")
async def vies_vat(
    country: str = Query(..., description="2-letter EU member state code, e.g. 'IE' (use 'EL' for Greece)"),
    vat: str = Query(..., description="VAT number without country prefix, e.g. '6388047V'"),
) -> JSONResponse:
    """GET /vies/vat?country=&vat= — validation TVA intracommunautaire (registre VIES)."""
    data = await lookup_vat(country, vat)
    return JSONResponse(content=data)


@router.get("/vies/health")
async def vies_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0), follow_redirects=True) as client:
            r = await client.get(VIES_BASE.format(country="IE", vat="6388047V"))
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "vies",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
