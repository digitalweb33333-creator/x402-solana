"""Endpoint 4 — Macro & Economic Snapshot (officiel EU, terrain ECB).

Snapshot macro DATÉ et structuré pour un agent de trading/recherche : pour chaque
indicateur demandé → valeur + as_of + next_release + source officielle. Plus un
REÇU SIGNÉ DATÉ du snapshot (backtest reproductible) = le différenciateur.

Angle (cf benchmark) : GlobalAPI/EconDash agrègent déjà du macro multi-pays à
$0.002–0.02 ; la donnée brute est gratuite donc pas de moat sur la breadth. Notre
wedge = focus Eurozone / ECB-aware (toutes les séries viennent de la BCE, officielle,
fiable), un champ `next_release` exploitable, et un snapshot signé reproductible.
NB : endpoint le PLUS contesté des 7 (cf BILAN) — à publier en connaissance de cause.

5 règles : verdict COMPLETE/PARTIAL/ABSTAIN, confidence + reasons[], data_freshness,
codes d'erreur, ABSTAIN si aucun indicateur disponible.

Source : ECB Data Portal SDMX 2.1 (data-api.ecb.europa.eu), sans clé. Tier $0.005. TTL 1 h.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.sources.http_util import TTLCache, client, get_json
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

ECB = "https://data-api.ecb.europa.eu/service/data"
SOURCE = "European Central Bank — Data Portal SDMX 2.1 (data-api.ecb.europa.eu)"
_cache = TTLCache(3600)

# Registre d'indicateurs → (dataset, clé SDMX, unité, area-aware, famille, cadence next_release).
INDICATORS: dict[str, dict[str, Any]] = {
    "inflation_hicp": {"dataset": "ICP", "key": "M.{area}.N.000000.4.ANR", "unit": "% YoY", "area": True,
                       "label": "HICP headline inflation (annual rate of change)",
                       "cadence": "monthly (flash ~end of reference month, final ~mid next month)"},
    "core_inflation": {"dataset": "ICP", "key": "M.{area}.N.XEF000.4.ANR", "unit": "% YoY", "area": True,
                       "label": "HICP core inflation, excl. energy & food",
                       "cadence": "monthly"},
    "unemployment": {"dataset": "LFSI", "key": "M.I9.S.UNEHRT.TOTAL0.15_74.T", "unit": "% of labour force", "area": False,
                     "label": "Euro area unemployment rate (15-74)", "cadence": "monthly (~early month, t+2)"},
    "deposit_facility_rate": {"dataset": "FM", "key": "D.U2.EUR.4F.KR.DFR.LEV", "unit": "% p.a.", "area": False,
                              "label": "ECB deposit facility rate (DFR)", "cadence": "per ECB Governing Council monetary-policy meeting"},
    "main_refi_rate": {"dataset": "FM", "key": "D.U2.EUR.4F.KR.MRR_FR.LEV", "unit": "% p.a.", "area": False,
                       "label": "ECB main refinancing operations rate", "cadence": "per ECB Governing Council monetary-policy meeting"},
    "marginal_lending_rate": {"dataset": "FM", "key": "D.U2.EUR.4F.KR.MLF.LEV", "unit": "% p.a.", "area": False,
                              "label": "ECB marginal lending facility rate", "cadence": "per ECB Governing Council monetary-policy meeting"},
    "fx_usd": {"dataset": "EXR", "key": "D.USD.EUR.SP00.A", "unit": "USD per EUR", "area": False,
               "label": "EUR/USD ECB reference rate", "cadence": "every TARGET working day ~16:00 CET"},
    "fx_gbp": {"dataset": "EXR", "key": "D.GBP.EUR.SP00.A", "unit": "GBP per EUR", "area": False,
               "label": "EUR/GBP ECB reference rate", "cadence": "every TARGET working day ~16:00 CET"},
    "fx_jpy": {"dataset": "EXR", "key": "D.JPY.EUR.SP00.A", "unit": "JPY per EUR", "area": False,
               "label": "EUR/JPY ECB reference rate", "cadence": "every TARGET working day ~16:00 CET"},
    "fx_chf": {"dataset": "EXR", "key": "D.CHF.EUR.SP00.A", "unit": "CHF per EUR", "area": False,
               "label": "EUR/CHF ECB reference rate", "cadence": "every TARGET working day ~16:00 CET"},
}
DEFAULT_INDICATORS = ["inflation_hicp", "deposit_facility_rate", "unemployment", "fx_usd"]
ECB_CALENDAR_URL = "https://www.ecb.europa.eu/press/calendars/statscal/html/index.en.html"
ECB_GC_CALENDAR_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"


def _parse_sdmx(payload: dict[str, Any]) -> tuple[float, str] | None:
    datasets = payload.get("dataSets") or []
    if not datasets:
        return None
    series = datasets[0].get("series") or {}
    if not series:
        return None
    skey = next(iter(series))
    obs = series[skey].get("observations") or {}
    if not obs:
        return None
    obs_dims = (((payload.get("structure") or {}).get("dimensions") or {}).get("observation")) or []
    time_values = obs_dims[0].get("values", []) if obs_dims else []
    idx = max(obs, key=lambda k: int(k))
    value = obs[idx][0]
    if value is None:
        return None
    try:
        date = time_values[int(idx)].get("id")
    except (IndexError, ValueError, AttributeError):
        date = None
    return float(value), date


def _next_release(spec: dict[str, Any]) -> dict[str, Any]:
    """Bloc next_release honnête : cadence + URL du calendrier officiel faisant foi."""
    family_meeting = "Governing Council" in spec["cadence"]
    return {
        "cadence": spec["cadence"],
        "authoritative_calendar": ECB_GC_CALENDAR_URL if family_meeting else ECB_CALENDAR_URL,
        "is_estimated": True,
        "note": "Exact date per the ECB official calendar (linked); cadence given for agent timing.",
    }


async def _fetch_indicator(name: str, area: str) -> dict[str, Any]:
    spec = INDICATORS[name]
    key = spec["key"].format(area=area) if spec["area"] else spec["key"]
    url = f"{ECB}/{spec['dataset']}/{key}"
    c = await client("ecb", timeout=12.0, headers={"User-Agent": "x402-endpoints/1.0", "Accept": "application/json"})
    data, err = await get_json(c, url, params={"format": "jsondata", "lastNObservations": 1})
    base = {"indicator": name, "label": spec["label"], "unit": spec["unit"],
            "series_key": f"{spec['dataset']}/{key}", "source": SOURCE,
            "next_release": _next_release(spec)}
    if err:
        return {**base, "status": "unavailable", "value": None, "as_of": None,
                "error_code": "SERIES_UNAVAILABLE", "detail": err}
    parsed = _parse_sdmx(data or {})
    if parsed is None:
        return {**base, "status": "unavailable", "value": None, "as_of": None,
                "error_code": "NO_OBSERVATION", "detail": "no observation in series"}
    value, obs_date = parsed
    return {**base, "status": "ok", "value": value, "as_of": obs_date}


async def snapshot(indicators: list[str], area: str) -> dict[str, Any]:
    requested = indicators or DEFAULT_INDICATORS
    unknown = [i for i in requested if i not in INDICATORS]
    known = [i for i in requested if i in INDICATORS]
    if not known:
        raise HTTPException(status_code=400, detail={
            "code": "NO_VALID_INDICATOR",
            "message": f"No valid indicator. Available: {', '.join(INDICATORS)}.",
            "unknown": unknown})

    area_code = (area or "U2").strip().upper()
    key = f"{area_code}|{','.join(sorted(known))}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    results = await asyncio.gather(*[_fetch_indicator(n, area_code) for n in known])
    ok = [r for r in results if r["status"] == "ok"]
    if not ok:
        # Toutes les séries connues sont muettes → source down, pas de charge.
        raise HTTPException(status_code=502, detail={
            "code": "ALL_SERIES_UNAVAILABLE",
            "message": "ECB Data Portal returned no observation for any requested indicator; not charged."})

    if len(ok) == len(known) and not unknown:
        verdict, confidence = "COMPLETE", 1.0
    else:
        verdict, confidence = "PARTIAL", clamp01(0.6 + 0.4 * len(ok) / max(1, len(requested)))

    reasons = [reason("INDICATORS_RETURNED", f"{len(ok)}/{len(requested)} indicators resolved from official ECB series", -0.5)]
    if unknown:
        reasons.append(reason("UNKNOWN_INDICATORS", f"Ignored unknown indicators: {', '.join(unknown)}", 0.1))
    missing = [r["indicator"] for r in results if r["status"] != "ok"]
    if missing:
        reasons.append(reason("SERIES_UNAVAILABLE", f"Unavailable this run: {', '.join(missing)}", 0.2))

    oldest_as_of = min((r["as_of"] for r in ok if r.get("as_of")), default=None)
    # Empreinte déterministe des valeurs (épingle le snapshot pour le backtest).
    h = hashlib.sha256()
    for r in sorted(ok, key=lambda x: x["indicator"]):
        h.update(f"{r['indicator']}={r['value']}@{r['as_of']};".encode())
    values_hash = h.hexdigest()[:16]
    receipt = sign_receipt({
        "kind": "macro_snapshot",
        "area": area_code,
        "verdict": verdict,
        "indicators": {r["indicator"]: {"value": r["value"], "as_of": r["as_of"]} for r in ok},
        "values_hash": values_hash,
        "snapshot_at": now_iso(),
    })

    shaped = {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "query": {"indicators": requested, "area": area_code},
        "indicators": {r["indicator"]: {k: r[k] for k in r if k != "indicator"} for r in results},
        "signed_snapshot_receipt": receipt,
        "data_freshness": freshness(oldest_as_of, deterministic=True, sources=[SOURCE],
                                    extra={"values_hash": values_hash, "resolved": len(ok), "requested": len(requested)}),
        "error": None,
        "timestamp": now_iso(),
        "disclaimer": "Official ECB reference data (informational). Reference rates/values, not live market quotes. Not investment advice.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/macro/snapshot")
async def macro_snapshot(
    indicators: str | None = Query(None, description="Comma-separated, e.g. 'inflation_hicp,deposit_facility_rate,unemployment,fx_usd'. Default = those 4."),
    area: str | None = Query("U2", description="HICP reference area: 'U2' (euro area, default) or a country code, e.g. 'FR','DE'"),
) -> JSONResponse:
    """GET /macro/snapshot — dated euro-area macro indicators (ECB) with next_release + signed snapshot receipt."""
    ind_list = [i.strip() for i in (indicators or "").split(",") if i.strip()]
    return JSONResponse(content=await snapshot(ind_list, area or "U2"))


@router.get("/macro/snapshot/health")
async def macro_snapshot_health() -> JSONResponse:
    c = await client("ecb", timeout=8.0, headers={"User-Agent": "x402-endpoints/1.0", "Accept": "application/json"})
    data, err = await get_json(c, f"{ECB}/EXR/D.USD.EUR.SP00.A", params={"format": "jsondata", "lastNObservations": 1})
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "macro-snapshot", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "available_indicators": list(INDICATORS), "cache_entries": len(_cache)})
