"""Endpoint — BCE taux de change (euro reference rates).

Wrapper du web service SDMX 2.1 officiel de la Banque Centrale Européenne :
renvoie le taux de change de référence EUR contre une devise (dernier taux dispo
ou à une date donnée). Parse proprement le SDMX-JSON (structure imbriquée).

⚠️ Ce sont des taux de RÉFÉRENCE BCE (informatifs, publiés ~16h00 CET chaque jour
ouvré), PAS des taux de marché temps réel.

Source : ECB Data Portal SDMX 2.1 (data-api.ecb.europa.eu), sans clé.
Tier $0.01. TTL 12h (taux journalier, 1 publication/jour ouvré).
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

BASE = "https://data-api.ecb.europa.eu/service/data/EXR"
SOURCE_NAME = "European Central Bank — euro reference rates, SDMX 2.1 (data-api.ecb.europa.eu)"
DISCLAIMER = "Taux de référence BCE (informatifs, publiés ~16h CET les jours ouvrés), pas un taux de marché temps réel."

_CCY_RE = re.compile(r"^[A-Z]{3}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FREQS = {"D", "M"}

_CACHE_TTL = 12 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(12.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_HEADERS = {"User-Agent": "x402-endpoints/1.0", "Accept": "application/json"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _parse_sdmx(payload: dict[str, Any]) -> tuple[float, str] | None:
    """Extrait (rate, observation_date) du SDMX-JSON. None si pas d'observation."""
    datasets = payload.get("dataSets") or []
    if not datasets:
        return None
    series = datasets[0].get("series") or {}
    if not series:
        return None
    skey = next(iter(series))
    observations = series[skey].get("observations") or {}
    if not observations:
        return None
    # Dimension temporelle des observations
    obs_dims = (((payload.get("structure") or {}).get("dimensions") or {}).get("observation")) or []
    time_values = obs_dims[0].get("values", []) if obs_dims else []
    # Dernière observation (clé d'index la plus élevée)
    idx = max(observations, key=lambda k: int(k))
    value = observations[idx][0]
    if value is None:
        return None
    date = None
    try:
        date = time_values[int(idx)].get("id")
    except (IndexError, ValueError, AttributeError):
        date = None
    return float(value), date


async def get_rate(currency: str | None, frequency: str | None, date: str | None) -> dict[str, Any]:
    ccy = (currency or "").strip().upper()
    if not _CCY_RE.match(ccy):
        raise HTTPException(status_code=400, detail="'currency' attendu : code ISO 3 lettres, ex. USD, GBP, CHF.")
    if ccy == "EUR":
        raise HTTPException(status_code=400, detail="La base est déjà EUR ; fournir une devise étrangère (ex. USD).")
    freq = (frequency or "D").strip().upper()
    if freq not in _FREQS:
        raise HTTPException(status_code=400, detail="'frequency' attendu : D (journalier) ou M (mensuel).")
    if date and not _DATE_RE.match(date.strip()):
        raise HTTPException(status_code=400, detail="'date' doit être une date ISO YYYY-MM-DD.")

    series_key = f"{freq}.{ccy}.EUR.SP00.A"
    if date:
        params = {"format": "jsondata", "startPeriod": date.strip(), "endPeriod": date.strip()}
        cache_key = f"{series_key}|{date.strip()}"
    else:
        params = {"format": "jsondata", "lastNObservations": 1}
        cache_key = f"{series_key}|latest"

    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.get(f"{BASE}/{series_key}", params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                # La BCE renvoie 404 pour une série inexistante (devise inconnue).
                if resp.status_code == 404:
                    raise HTTPException(status_code=404, detail=f"Aucune série de taux BCE pour la devise '{ccy}' (devise inconnue ?).")
                if resp.status_code == 200:
                    parsed = _parse_sdmx(resp.json())
                    if parsed is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Pas de taux de référence pour {ccy}" + (f" à la date {date}" if date else "") + " (jour non ouvré ?).")
                    rate, obs_date = parsed
                    shaped = {
                        "currency": ccy, "base": "EUR", "rate": rate,
                        "observation_date": obs_date, "frequency": freq,
                        "series_key": series_key,
                        "source": SOURCE_NAME,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "disclaimer": DISCLAIMER,
                    }
                    _cache[cache_key] = (time.time(), shaped)
                    return {**shaped, "cached": False}
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="Requête BCE invalide (devise/fréquence/date).")
                last_exc = httpx.HTTPStatusError(
                    f"ECB HTTP {resp.status_code}", request=resp.request, response=resp)
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant la BCE.")
    raise HTTPException(status_code=502, detail="Service BCE (data-api.ecb.europa.eu) indisponible.")


@router.get("/ecb/exchange-rate")
async def ecb_exchange_rate(
    currency: str = Query(..., description="3-letter ISO currency code, e.g. 'USD', 'GBP', 'CHF', 'JPY'"),
    frequency: str = Query("D", description="D (daily, default) or M (monthly)"),
    date: str | None = Query(None, description="Optional observation date ISO YYYY-MM-DD; default = latest available"),
) -> JSONResponse:
    """GET /ecb/exchange-rate — taux de référence EUR de la BCE pour une devise."""
    data = await get_rate(currency, frequency, date)
    return JSONResponse(content=data)


@router.get("/ecb/health")
async def ecb_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0), headers=_HEADERS) as client:
            r = await client.get(f"{BASE}/D.USD.EUR.SP00.A", params={"format": "jsondata", "lastNObservations": 1})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "ecb-exchange-rate",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
