"""Endpoint 7 — Cross-Exchange Signal Fusion (signal directionnel fusionné).

UN appel → verdict LONG/SHORT/NEUTRAL + confidence, fusionnant 4 composantes
structurées : (1) funding rate cross-exchange, (2) crowding bias (long/short),
(3) régime trend/chop, (4) lead-lag BTC→alt. Avec data_freshness déclarée (un signal
périmé est inutile).

Angle (cf benchmark) : Agent Signals vend ces 4 nombres SÉPARÉMENT et laisse la
synthèse à l'agent ; Pragma fusionne mais BTC-only à $0.25. Notre wedge = la FUSION
en un appel pour n'importe quel alt, moins cher. « Eux vendent 4 nombres ; nous
vendons la décision. »

5 règles : verdict en haut, confidence + reasons[], composantes structurées,
data_freshness/deterministic/sources, codes d'erreur, ABSTAIN si données exchange indisponibles.

Sources : Binance/Bybit/OKX/Hyperliquid (perp + klines, publics, gratuits). Tier $0.01–0.05. TTL 30 s.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.routers.derivatives_radar import _binance, _bybit, _hyperliquid, _okx
from app.sources.http_util import TTLCache, client, get_json
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = ["Binance/Bybit/OKX/Hyperliquid perp funding & OI", "Binance/Bybit klines (regime & lead-lag)"]
_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,15}$")
_cache = TTLCache(30)
_INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _klines(sym: str, interval: str, limit: int = 50) -> list[float] | None:
    """Closes récents pour `sym`USDT. Binance puis Bybit en fallback."""
    c = await client("binance", timeout=8.0)
    data, err = await get_json(c, "https://fapi.binance.com/fapi/v1/klines",
                               params={"symbol": f"{sym}USDT", "interval": interval, "limit": limit})
    if not err and isinstance(data, list) and data:
        return [float(k[4]) for k in data if len(k) > 4]
    # fallback Bybit (intervalle en minutes)
    mins = _INTERVAL_MIN.get(interval, 60)
    cb = await client("bybit", timeout=8.0)
    d2, e2 = await get_json(cb, "https://api.bybit.com/v5/market/kline",
                            params={"category": "linear", "symbol": f"{sym}USDT", "interval": str(mins), "limit": limit})
    if not e2:
        lst = (((d2 or {}).get("result") or {}).get("list")) or []
        # Bybit renvoie du plus récent au plus ancien → inverser, close = index 4
        closes = [float(r[4]) for r in reversed(lst) if len(r) > 4]
        return closes or None
    return None


def _returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1]]


def _efficiency_ratio(closes: list[float]) -> float | None:
    """Kaufman ER : |net| / somme(|pas|). ~1 = trend pur, ~0 = chop."""
    if len(closes) < 5:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return (net / path) if path else 0.0


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((x - mb) ** 2 for x in b) ** 0.5
    return (cov / (va * vb)) if (va and vb) else None


def _component_funding(venues: list[dict]) -> dict[str, Any]:
    frs = [v["funding_rate"] for v in venues if v.get("funding_rate") is not None]
    if not frs:
        return {"available": False, "vote": 0.0, "detail": "no funding data"}
    avg = sum(frs) / len(frs)
    # funding élevé positif = longs surpayent = biais contrarian baissier (et inversement).
    vote = -clamp01(abs(avg) / 0.0005) * (1 if avg > 0 else -1)
    bias = "crowded_long_pays" if avg > 0.0002 else "crowded_short_pays" if avg < -0.0002 else "neutral"
    return {"available": True, "avg_funding_rate": round(avg, 8), "per_venue": {v["venue"]: v.get("funding_rate") for v in venues},
            "bias": bias, "vote": round(vote, 3),
            "interpretation": "High positive funding → contrarian bearish; negative → contrarian bullish."}


def _component_crowding(venues: list[dict]) -> dict[str, Any]:
    ls = [v["long_short_ratio"] for v in venues if v.get("long_short_ratio") is not None]
    if not ls:
        return {"available": False, "vote": 0.0, "detail": "no long/short data"}
    avg = sum(ls) / len(ls)
    # crowded long (ratio>1) → contrarian short ; crowded short → contrarian long.
    vote = -clamp01(abs(avg - 1.0) / 1.0) * (1 if avg > 1 else -1)
    state = "crowded_long" if avg > 1.5 else "crowded_short" if avg < 0.67 else "balanced"
    return {"available": True, "avg_long_short_ratio": round(avg, 3), "state": state, "vote": round(vote, 3),
            "interpretation": "Crowded long → contrarian short bias; crowded short → contrarian long bias."}


def _component_regime(closes: list[float] | None) -> dict[str, Any]:
    if not closes or len(closes) < 10:
        return {"available": False, "vote": 0.0, "detail": "insufficient klines"}
    er = _efficiency_ratio(closes)
    direction = 1 if closes[-1] > closes[0] else -1
    regime = "trend" if (er or 0) >= 0.4 else "chop" if (er or 0) < 0.25 else "transitional"
    # momentum : suit la tendance, pondéré par la « pureté » du trend.
    vote = direction * (er or 0.0) if regime != "chop" else 0.0
    return {"available": True, "regime": regime, "efficiency_ratio": round(er, 3) if er is not None else None,
            "direction": "up" if direction > 0 else "down", "vote": round(vote, 3),
            "interpretation": "Trend regime → momentum vote follows direction; chop → no momentum signal."}


def _component_leadlag(alt_closes: list[float] | None, btc_closes: list[float] | None, sym: str) -> dict[str, Any]:
    if not alt_closes or not btc_closes or len(alt_closes) < 8 or len(btc_closes) < 8:
        return {"available": False, "vote": 0.0, "detail": "insufficient klines"}
    alt_r, btc_r = _returns(alt_closes), _returns(btc_closes)
    if sym.upper() == "BTC":
        return {"available": True, "is_btc": True, "lead_lag_corr": None, "vote": 0.0,
                "interpretation": "Symbol is BTC — no BTC→alt lead-lag."}
    # BTC mène d'une bougie : corr(alt[t], btc[t-1]).
    lag_corr = _corr(alt_r[1:], btc_r[:-1])
    btc_recent = (btc_closes[-1] / btc_closes[-6] - 1.0) if len(btc_closes) >= 6 and btc_closes[-6] else 0.0
    vote = clamp01(abs(btc_recent) / 0.03) * (1 if btc_recent > 0 else -1) * max(0.0, lag_corr or 0.0)
    return {"available": True, "lead_lag_corr": round(lag_corr, 3) if lag_corr is not None else None,
            "btc_recent_return_pct": round(btc_recent * 100, 2), "vote": round(vote, 3),
            "interpretation": "If BTC leads this alt (positive lag corr), propagate BTC's recent direction."}


async def fuse(symbol: str, timeframe: str) -> dict[str, Any]:
    sym = (symbol or "BTC").strip().upper()
    if not _SYM_RE.match(sym):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SYMBOL",
                            "message": "'symbol' must be a coin ticker, e.g. 'BTC','ETH','SOL'."})
    tf = (timeframe or "1h").strip().lower()
    if tf not in _INTERVAL_MIN:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEFRAME",
                            "message": f"'timeframe' must be one of {', '.join(_INTERVAL_MIN)}."})
    key = f"{sym}|{tf}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    venues_r, alt_klines, btc_klines = await asyncio.gather(
        asyncio.gather(_binance(sym), _bybit(sym), _okx(sym), _hyperliquid(sym)),
        _klines(sym, tf, 50),
        _klines("BTC", tf, 50),
    )
    venues = [v for v in venues_r if v]
    if not venues and not alt_klines:
        raise HTTPException(status_code=502, detail={"code": "EXCHANGE_DATA_UNAVAILABLE",
                            "message": "No exchange perp or kline data reachable for this symbol; not charged."})

    funding = _component_funding(venues)
    crowding = _component_crowding(venues)
    regime = _component_regime(alt_klines)
    leadlag = _component_leadlag(alt_klines, btc_klines, sym)
    components = {"funding": funding, "crowding": crowding, "regime": regime, "lead_lag": leadlag}

    # Fusion pondérée des votes disponibles.
    weights = {"funding": 0.30, "crowding": 0.25, "regime": 0.30, "lead_lag": 0.15}
    avail = {k: c for k, c in components.items() if c.get("available")}
    if not avail:
        # Droit de s'abstenir : composantes présentes mais aucune exploitable.
        return {"verdict": "ABSTAIN", "confidence": 0.3,
                "reasons": [reason("NO_USABLE_COMPONENT", "Exchange data reachable but no component computable", 0.4)],
                "query": {"symbol": sym, "timeframe": tf}, "components": components,
                "data_freshness": freshness(now_iso(), deterministic=False, sources=SOURCES,
                                            extra={"venues": [v["venue"] for v in venues]}),
                "error": {"code": "INSUFFICIENT_SIGNAL", "message": "No component usable for a directional call."},
                "timestamp": now_iso(),
                "disclaimer": "Market data for information only, not financial advice."}
    wsum = sum(weights[k] for k in avail)
    net = sum(components[k]["vote"] * weights[k] for k in avail) / wsum  # ∈ [-1,1]

    if net >= 0.18:
        verdict = "LONG"
    elif net <= -0.18:
        verdict = "SHORT"
    else:
        verdict = "NEUTRAL"
    confidence = clamp01(0.4 + abs(net) * 0.8 + 0.05 * len(avail))

    reasons = [reason(f"{k.upper()}_VOTE", f"{k} vote {components[k]['vote']:+.2f}", components[k]["vote"])
               for k in avail]
    reasons.append(reason("FUSED_SCORE", f"Fused directional score {net:+.2f} from {len(avail)}/4 components", net))

    shaped = {
        "verdict": verdict, "confidence": round(confidence, 3), "fused_score": round(net, 3),
        "reasons": reasons,
        "query": {"symbol": sym, "timeframe": tf},
        "components": components,
        "component_weights": weights,
        "venues_used": [v["venue"] for v in venues],
        "data_freshness": freshness(now_iso(), deterministic=False, sources=SOURCES,
                                    extra={"components_available": list(avail), "venues": [v["venue"] for v in venues]}),
        "error": None, "timestamp": now_iso(),
        "disclaimer": "Fused directional signal from public market data, not financial advice. A signal is not a recommendation.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/signal-fusion")
async def signal_fusion(
    symbol: str = Query("BTC", description="Coin ticker, e.g. 'BTC','ETH','SOL'"),
    timeframe: str = Query("1h", description="Regime/lead-lag timeframe: 5m | 15m | 1h | 4h"),
) -> JSONResponse:
    """GET /crypto/signal-fusion — one-call LONG/SHORT/NEUTRAL fusing funding + crowding + regime + BTC→alt lead-lag."""
    return JSONResponse(content=await fuse(symbol, timeframe))


@router.get("/crypto/signal-fusion/health")
async def signal_fusion_health() -> JSONResponse:
    k = await _klines("BTC", "1h", 5)
    ok = bool(k)
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "signal-fusion", "status": "ok" if ok else "degraded",
        "upstream": {"klines_reachable": ok}, "cache_entries": len(_cache)})
