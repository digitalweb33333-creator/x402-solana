"""Endpoint — Derivatives radar (perp funding / OI / long-short, multi-venue).

Differentiator: aggregates perp derivatives from 4 venues (Binance, Bybit, OKX,
Hyperliquid) in one call AND computes the cross-venue FUNDING SPREAD + arbitrage
direction + a crowding signal — which the raw feeds do not give.

Sources (public, free, keyless):
- Binance Futures (fapi.binance.com) — funding, OI, global long/short.
- Bybit v5 (api.bybit.com) — funding, OI.
- OKX v5 (okx.com) — funding, OI.
- Hyperliquid (api.hyperliquid.xyz) — funding, OI, mark.

Resilience: if a venue geo-blocks the datacenter IP (e.g. Binance/US -> 451), the
others fill in. Returns 200 as soon as AT LEAST one venue responds.

"computed" tier $0.05. TTL 30 s (funding/OI move fast).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache, client, get_json, post_json, utc_now

router = APIRouter()

SOURCE = "Binance + Bybit + OKX + Hyperliquid (perp public APIs)"
_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,15}$")
_cache = TTLCache(30)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _binance(sym: str) -> dict | None:
    c = await client("binance", timeout=8.0)
    pair = f"{sym}USDT"
    pi, e1 = await get_json(c, "https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": pair})
    if e1 or not isinstance(pi, dict):
        return None
    oi, _ = await get_json(c, "https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": pair})
    ls, _ = await get_json(c, "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                           params={"symbol": pair, "period": "5m", "limit": 1})
    mark = _num(pi.get("markPrice"))
    oi_base = _num((oi or {}).get("openInterest"))
    out = {"venue": "binance", "funding_rate": _num(pi.get("lastFundingRate")),
           "mark_price": mark,
           "open_interest_base": oi_base,
           "open_interest_usd": (oi_base * mark) if (oi_base and mark) else None}
    if isinstance(ls, list) and ls:
        out["long_short_ratio"] = _num(ls[0].get("longShortRatio"))
    return out


async def _bybit(sym: str) -> dict | None:
    c = await client("bybit", timeout=8.0)
    data, err = await get_json(c, "https://api.bybit.com/v5/market/tickers",
                               params={"category": "linear", "symbol": f"{sym}USDT"})
    if err:
        return None
    lst = (((data or {}).get("result") or {}).get("list")) or []
    if not lst:
        return None
    t = lst[0]
    mark = _num(t.get("markPrice"))
    oi_base = _num(t.get("openInterest"))
    return {"venue": "bybit", "funding_rate": _num(t.get("fundingRate")),
            "mark_price": mark, "open_interest_base": oi_base,
            "open_interest_usd": _num(t.get("openInterestValue")) or ((oi_base * mark) if (oi_base and mark) else None)}


async def _okx(sym: str) -> dict | None:
    c = await client("okx", timeout=8.0)
    inst = f"{sym}-USDT-SWAP"
    fr, e1 = await get_json(c, "https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
    if e1:
        return None
    frd = ((fr or {}).get("data") or [{}])[0]
    oi, _ = await get_json(c, "https://www.okx.com/api/v5/public/open-interest", params={"instId": inst})
    oid = ((oi or {}).get("data") or [{}])[0]
    return {"venue": "okx", "funding_rate": _num(frd.get("fundingRate")),
            "mark_price": None, "open_interest_base": _num(oid.get("oi")),
            "open_interest_usd": _num(oid.get("oiCcy"))}


async def _hyperliquid(sym: str) -> dict | None:
    c = await client("hyperliquid", timeout=8.0)
    data, err = await post_json(c, "https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"})
    if err or not isinstance(data, list) or len(data) < 2:
        return None
    meta, ctxs = data[0], data[1]
    universe = (meta or {}).get("universe") or []
    idx = next((i for i, a in enumerate(universe) if (a.get("name") or "").upper() == sym.upper()), None)
    if idx is None or idx >= len(ctxs):
        return None
    ctx = ctxs[idx]
    mark = _num(ctx.get("markPx"))
    oi_base = _num(ctx.get("openInterest"))
    return {"venue": "hyperliquid", "funding_rate": _num(ctx.get("funding")),
            "mark_price": mark, "open_interest_base": oi_base,
            "open_interest_usd": (oi_base * mark) if (oi_base and mark) else None}


async def radar(symbol: str) -> dict[str, Any]:
    sym = (symbol or "BTC").strip().upper()
    if not _SYM_RE.match(sym):
        raise HTTPException(status_code=400, detail="'symbol' must be a coin ticker, e.g. 'BTC', 'ETH', 'SOL'.")

    cached = _cache.get(sym)
    if cached is not None:
        return {**cached, "cached": True}

    results = await asyncio.gather(_binance(sym), _bybit(sym), _okx(sym), _hyperliquid(sym))
    venues = [v for v in results if v]
    if not venues:
        raise HTTPException(status_code=502, detail="No derivatives venue reachable for this symbol; not charged.")

    # --- Differentiator: cross-venue funding spread + arbitrage ---
    fundings = [(v["venue"], v["funding_rate"]) for v in venues if v.get("funding_rate") is not None]
    spread = None
    if len(fundings) >= 2:
        hi = max(fundings, key=lambda x: x[1])
        lo = min(fundings, key=lambda x: x[1])
        spread = {
            "funding_spread": round(hi[1] - lo[1], 8),
            "long_on": lo[0],   # lowest funding paid -> open long here
            "short_on": hi[0],  # highest funding paid -> open short here
            "annualized_spread_pct": round((hi[1] - lo[1]) * 3 * 365 * 100, 2),  # ~3 fundings/day
            "note": "Long the venue with the lowest funding, short the highest, to capture the spread.",
        }
    total_oi = sum(v["open_interest_usd"] for v in venues if v.get("open_interest_usd"))
    ls_vals = [v["long_short_ratio"] for v in venues if v.get("long_short_ratio") is not None]
    avg_ls = round(sum(ls_vals) / len(ls_vals), 3) if ls_vals else None
    crowding = None
    if avg_ls is not None:
        crowding = "crowded_long" if avg_ls > 1.5 else "crowded_short" if avg_ls < 0.67 else "balanced"

    shaped = {
        "query": {"symbol": sym},
        "venues": venues,
        "venues_count": len(venues),
        "aggregate": {
            "total_open_interest_usd": round(total_oi) if total_oi else None,
            "avg_long_short_ratio": avg_ls,
            "crowding": crowding,
        },
        "funding_arbitrage": spread,
        "sources_ok": {v["venue"]: True for v in venues},
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Market data for information only, not financial advice.",
    }
    _cache.set(sym, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/derivatives-radar")
async def derivatives_radar(
    symbol: str = Query("BTC", description="Coin ticker, e.g. 'BTC', 'ETH', 'SOL'"),
) -> JSONResponse:
    """GET /crypto/derivatives-radar — multi-venue perp funding/OI/long-short + computed funding-arbitrage spread."""
    return JSONResponse(content=await radar(symbol))


@router.get("/crypto/derivatives-radar/health")
async def derivatives_radar_health() -> JSONResponse:
    res = await asyncio.gather(_binance("BTC"), _bybit("BTC"), _okx("BTC"), _hyperliquid("BTC"))
    up = {n: bool(v) for n, v in zip(("binance", "bybit", "okx", "hyperliquid"), res)}
    ok = any(up.values())
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "derivatives-radar", "status": "ok" if ok else "degraded",
        "venues_reachable": up, "source": SOURCE, "cache_entries": len(_cache)})
