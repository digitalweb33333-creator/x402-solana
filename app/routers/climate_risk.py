"""Endpoint — Climate risk score (score de risque climatique dérivé).

Calcule un score de risque climatique COMPOSITE (0-100) à partir des projections
CMIP6 downscalées d'Open-Meteo (modèles HighResMIP, données quotidiennes
1950-2050). Le score agrège trois sous-aléas via une formule DÉTERMINISTE et
documentée : chaleur, sécheresse, pluie extrême — comparés entre une fenêtre de
référence (baseline) et un horizon futur choisi.

Source : Open-Meteo Climate API (climate-api.open-meteo.com), CC BY 4.0, sans clé.

HONNÊTETÉ (cf description) : il s'agit d'un score DÉRIVÉ / heuristique calculé à
partir de données climatiques officielles, PAS une notation de risque
réglementaire ni certifiée. De plus, le jeu de données Open-Meteo CMIP6
(HighResMIP) fournit une UNIQUE trajectoire haute-émission jusqu'en 2050 et NE
différencie PAS les scénarios SSP : le paramètre `scenario` est accepté et
renvoyé à titre informatif mais N'ALTÈRE PAS la projection sous-jacente (signalé
explicitement dans `methodology.scenario_note`). Aucun contournement : la
limitation de la source est exposée telle quelle.

Tier $0.05 (donnée calculée à valeur ajoutée). TTL 30 jours (projections quasi statiques).
"""

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

CLIMATE_URL = "https://climate-api.open-meteo.com/v1/climate"
SOURCE_NAME = "Open-Meteo Climate API — CMIP6 HighResMIP downscaled (climate-api.open-meteo.com)"
ATTRIBUTION = "Weather/climate data by Open-Meteo.com (CC BY 4.0)"

# Ensemble de modèles HighResMIP downscalés exposés par Open-Meteo.
_MODELS = ["MRI_AGCM3_2_S", "EC_Earth3P_HR", "CMCC_CM2_VHR4",
           "MPI_ESM1_2_XR", "NICAM16_8S", "FGOALS_f3_H", "HiRAM_SIT_HR"]

# Fenêtre de référence (climat récent observable dans le jeu de données).
_BASELINE = ("2005-01-01", "2014-12-31")
_HORIZON_MIN, _HORIZON_MAX = 2030, 2050
_WINDOW_YEARS = 10
_ALLOWED_SCENARIOS = {"ssp245", "ssp585"}

# --- Seuils de normalisation (DÉTERMINISTES, documentés) ---
_HEAT30_FULL = 100.0   # 100 j/an > 30°C -> saturation de la composante "jours chauds"
_HEAT35_FULL = 30.0    # 30 j/an > 35°C -> saturation de la composante "jours très chauds"
_DROUGHT_FULL = 60.0   # plus longue série sèche de 60 j -> saturation
_RAIN_FULL = 15.0      # 15 j/an >= 20 mm -> saturation
_DRY_MM = 1.0          # un jour est "sec" si precip < 1 mm
_EXTREME_RAIN_MM = 20.0
_HOT_C = 30.0
_VERY_HOT_C = 35.0
# Pondérations du composite (somme = 1).
_W_HEAT, _W_DROUGHT, _W_RAIN = 0.45, 0.30, 0.25

_CACHE_TTL = 30 * 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(45.0, connect=6.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.8
_HEADERS = {"User-Agent": "x402-endpoints/1.0 (climate risk score)"}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _longest_dry_spell(precip: list[float | None]) -> int:
    best = cur = 0
    for p in precip:
        if p is not None and p < _DRY_MM:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _annual_hazards(times: list[str], tmax: list[float | None], precip: list[float | None]) -> dict[str, float] | None:
    """Métriques annuelles moyennes pour UN modèle sur la fenêtre (déterministe)."""
    by_year: dict[str, dict[str, Any]] = {}
    for i, t in enumerate(times):
        year = t[:4]
        slot = by_year.setdefault(year, {"tmax": [], "precip": []})
        slot["tmax"].append(tmax[i] if i < len(tmax) else None)
        slot["precip"].append(precip[i] if i < len(precip) else None)

    years_hot, years_vhot, years_dry, years_rain = [], [], [], []
    for slot in by_year.values():
        tx = slot["tmax"]
        pr = slot["precip"]
        if not any(v is not None for v in tx):
            continue
        years_hot.append(sum(1 for v in tx if v is not None and v > _HOT_C))
        years_vhot.append(sum(1 for v in tx if v is not None and v > _VERY_HOT_C))
        years_dry.append(_longest_dry_spell(pr))
        years_rain.append(sum(1 for v in pr if v is not None and v >= _EXTREME_RAIN_MM))
    if not years_hot:
        return None
    n = len(years_hot)
    return {
        "hot_days": sum(years_hot) / n,
        "very_hot_days": sum(years_vhot) / n,
        "longest_dry_spell": sum(years_dry) / n,
        "extreme_rain_days": sum(years_rain) / n,
    }


def _ensemble(daily: dict[str, Any]) -> dict[str, float] | None:
    """Moyenne d'ensemble des métriques annuelles sur tous les modèles disponibles."""
    times = daily.get("time", [])
    per_model = []
    for model in _MODELS:
        tmax = daily.get(f"temperature_2m_max_{model}")
        precip = daily.get(f"precipitation_sum_{model}")
        if not tmax or not precip:
            continue
        metrics = _annual_hazards(times, tmax, precip)
        if metrics is not None:
            per_model.append(metrics)
    if not per_model:
        return None
    keys = per_model[0].keys()
    return {k: sum(m[k] for m in per_model) / len(per_model) for k in keys} | {"models_used": len(per_model)}


def _score_window(metrics: dict[str, float]) -> dict[str, Any]:
    heat = _clamp(70.0 * (metrics["hot_days"] / _HEAT30_FULL)
                  + 30.0 * (metrics["very_hot_days"] / _HEAT35_FULL))
    drought = _clamp(100.0 * (metrics["longest_dry_spell"] / _DROUGHT_FULL))
    rain = _clamp(100.0 * (metrics["extreme_rain_days"] / _RAIN_FULL))
    composite = _clamp(_W_HEAT * heat + _W_DROUGHT * drought + _W_RAIN * rain)
    return {
        "composite_score": round(composite, 1),
        "rating": _rating(composite),
        "subscores": {
            "heat": round(heat, 1),
            "drought": round(drought, 1),
            "extreme_rain": round(rain, 1),
        },
        "indicators": {
            "mean_days_above_30C_per_year": round(metrics["hot_days"], 1),
            "mean_days_above_35C_per_year": round(metrics["very_hot_days"], 1),
            "mean_longest_dry_spell_days": round(metrics["longest_dry_spell"], 1),
            "mean_days_rain_over_20mm_per_year": round(metrics["extreme_rain_days"], 1),
        },
        "models_used": int(metrics["models_used"]),
    }


def _rating(score: float) -> str:
    if score < 25:
        return "Low"
    if score < 50:
        return "Moderate"
    if score < 75:
        return "High"
    return "Severe"


async def _fetch_window(client: httpx.AsyncClient, lat: float, lon: float,
                        start: str, end: str) -> dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "models": ",".join(_MODELS), "daily": "temperature_2m_max,precipitation_sum",
    }
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.get(CLIMATE_URL, params=params)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 200:
                return resp.json().get("daily", {})
            if resp.status_code == 400:
                reason = ""
                try:
                    reason = resp.json().get("reason", "")
                except ValueError:
                    pass
                raise HTTPException(status_code=400, detail=f"Open-Meteo a rejeté la requête: {reason or 'paramètres invalides'}.")
            last_exc = httpx.HTTPStatusError(
                f"Open-Meteo HTTP {resp.status_code}", request=resp.request, response=resp)
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant Open-Meteo Climate.")
    raise HTTPException(status_code=502, detail="Service Open-Meteo Climate indisponible.")


def _methodology(scenario: str, baseline: tuple[str, str], future: tuple[str, str]) -> dict[str, Any]:
    return {
        "summary": (
            "Composite 0-100 = 0.45*heat + 0.30*drought + 0.25*extreme_rain, each subscore "
            "normalised from CMIP6 daily projections (ensemble mean of Open-Meteo HighResMIP models). "
            "heat = 70*(days>30C/100/yr) + 30*(days>35C/30/yr); drought = 100*(longest dry spell/60d); "
            "extreme_rain = 100*(days>=20mm/15/yr). Ratings: Low<25, Moderate<50, High<75, Severe>=75."
        ),
        "baseline_window": f"{baseline[0]}..{baseline[1]}",
        "future_window": f"{future[0]}..{future[1]}",
        "deterministic": True,
        "scenario_requested": scenario,
        "scenario_note": (
            "Open-Meteo's CMIP6 HighResMIP dataset provides a single high-emissions pathway to 2050 and "
            "does NOT expose per-SSP selection; the 'scenario' value is echoed for reference only and does "
            "not change the underlying projection."
        ),
        "data_disclaimer": "Derived/heuristic score from official climate projections — not an official or regulatory risk rating.",
    }


async def compute_score(lat: float | None, lon: float | None, scenario: str, horizon: int) -> dict[str, Any]:
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="'lat' et 'lon' sont requis.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="'lat'/'lon' hors bornes.")
    scen = (scenario or "ssp245").strip().lower()
    if scen not in _ALLOWED_SCENARIOS:
        raise HTTPException(status_code=400, detail="'scenario' attendu: ssp245 | ssp585.")
    if not (_HORIZON_MIN <= horizon <= _HORIZON_MAX):
        raise HTTPException(status_code=400, detail=f"'horizon' attendu dans [{_HORIZON_MIN}, {_HORIZON_MAX}].")

    fut_end = min(horizon, _HORIZON_MAX)
    fut_start = max(fut_end - (_WINDOW_YEARS - 1), 2015)
    future = (f"{fut_start}-01-01", f"{fut_end}-12-31")

    cache_key = f"{round(lat, 4)}|{round(lon, 4)}|{scen}|{horizon}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        base_daily = await _fetch_window(client, lat, lon, *_BASELINE)
        fut_daily = await _fetch_window(client, lat, lon, *future)

    base_metrics = _ensemble(base_daily)
    fut_metrics = _ensemble(fut_daily)
    if base_metrics is None or fut_metrics is None:
        raise HTTPException(
            status_code=404,
            detail="Aucune donnée climatique CMIP6 disponible pour ces coordonnées (ex. océan / hors couverture).")

    baseline_block = _score_window(base_metrics)
    future_block = _score_window(fut_metrics)
    delta = round(future_block["composite_score"] - baseline_block["composite_score"], 1)

    shaped = {
        "query": {"lat": lat, "lon": lon, "scenario": scen, "horizon": horizon},
        "composite_score": future_block["composite_score"],
        "rating": future_block["rating"],
        "subscores": future_block["subscores"],
        "indicators": future_block["indicators"],
        "baseline": {
            "window": f"{_BASELINE[0][:4]}-{_BASELINE[1][:4]}",
            "composite_score": baseline_block["composite_score"],
            "rating": baseline_block["rating"],
            "subscores": baseline_block["subscores"],
            "indicators": baseline_block["indicators"],
        },
        "delta_vs_baseline": delta,
        "scenario": scen,
        "horizon": horizon,
        "methodology": _methodology(scen, _BASELINE, future),
        "source": SOURCE_NAME,
        "attribution": ATTRIBUTION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": "Score dérivé/heuristique à partir des projections CMIP6 (Open-Meteo), pas une notation de risque officielle.",
    }
    _cache[cache_key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/climate-risk/score")
async def climate_risk_score(
    lat: float | None = Query(None, description="Latitude, e.g. 48.85"),
    lon: float | None = Query(None, description="Longitude, e.g. 2.35"),
    scenario: str = Query("ssp245", description="Emission scenario label: ssp245 | ssp585 (informational, see methodology)"),
    horizon: int = Query(2050, description="Future target year [2030-2050], e.g. 2050"),
) -> JSONResponse:
    """GET /climate-risk/score — score de risque climatique composite dérivé (Open-Meteo CMIP6)."""
    data = await compute_score(lat, lon, scenario, horizon)
    return JSONResponse(content=data)


@router.get("/climate-risk/health")
async def climate_risk_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), headers=_HEADERS) as client:
            r = await client.get(CLIMATE_URL, params={
                "latitude": 48.85, "longitude": 2.35, "start_date": "2049-01-01",
                "end_date": "2049-01-03", "models": "MRI_AGCM3_2_S", "daily": "temperature_2m_max"})
            upstream_ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "climate-risk",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "cache_entries": len(_cache),
        },
    )
