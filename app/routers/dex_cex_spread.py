"""Endpoint — DEX vs CEX spread (arbitrage, net after fees).

Differentiator: does not return two raw prices but the NET SPREAD after fees
(CEX taker + DEX swap) + the execution direction + whether it is profitable.
The agent just has to execute.

Sources (public, free, keyless):
- DexScreener (search) — best DEX price (most liquid pair).
- Binance / Bybit spot — CEX price (multi-venue, resilient to geo-block).

"computed" tier $0.05. TTL 20 s.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache, client, get_json, utc_now

router = APIRouter()

SOURCE = "DexScreener (DEX) + Binance/Bybit spot (CEX)"
_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,15}$")
_cache = TTLCache(20)

# Fee assumptions (transparent, the agent can re-adjust on read)
CEX_TAKER_FEE_PCT = 0.10
DEX_SWAP_FEE_PCT = 0.30


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _dex_price(sym: str, anchor: float | None) -> dict | None:
    """Real DEX price. Symbol search also returns SCAMS named like the token (fake
    liquidity, wrong price). We anchor on the CEX price: keep the most liquid pair whose
    price is consistent (+/-25%) with the CEX; otherwise the closest one."""
    c = await client("dexscreener", timeout=10.0)
    data, err = await get_json(c, "https://api.dexscreener.com/latest/dex/search", params={"q": sym})
    pairs = (data or {}).get("pairs") or []
    base = sym.upper().lstrip("W")
    usd_pairs = [p for p in pairs if _num(p.get("priceUsd"))
                 and (p.get("baseToken") or {}).get("symbol", "").upper().lstrip("W") == base]
    if not usd_pairs:
        return None
    if anchor:
        consistent = [p for p in usd_pairs if abs(_num(p["priceUsd"]) - anchor) / anchor <= 0.10]
        if consistent:
            best = max(consistent, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
        else:  # no consistent price -> the closest to the CEX (anti-scam)
            best = min(usd_pairs, key=lambda p: abs(_num(p["priceUsd"]) - anchor))
    else:
        best = max(usd_pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
    return {"price_usd": _num(best.get("priceUsd")), "dex": best.get("dexId"),
            "chain": best.get("chainId"), "liquidity_usd": _num((best.get("liquidity") or {}).get("usd")),
            "pair_url": best.get("url")}


async def _binance_price(sym: str) -> dict | None:
    c = await client("binance", timeout=8.0)
    data, err = await get_json(c, "https://api.binance.com/api/v3/ticker/price", params={"symbol": f"{sym}USDT"})
    p = _num((data or {}).get("price")) if not err else None
    return {"venue": "binance", "price_usd": p} if p else None


async def _bybit_price(sym: str) -> dict | None:
    c = await client("bybit", timeout=8.0)
    data, err = await get_json(c, "https://api.bybit.com/v5/market/tickers",
                               params={"category": "spot", "symbol": f"{sym}USDT"})
    lst = (((data or {}).get("result") or {}).get("list")) or []
    p = _num(lst[0].get("lastPrice")) if lst else None
    return {"venue": "bybit", "price_usd": p} if p else None


async def spread(symbol: str) -> dict[str, Any]:
    sym = (symbol or "ETH").strip().upper()
    if not _SYM_RE.match(sym):
        raise HTTPException(status_code=400, detail="'symbol' must be a coin ticker, e.g. 'ETH', 'WBTC', 'ARB'.")

    cached = _cache.get(sym)
    if cached is not None:
        return {**cached, "cached": True}

    import asyncio
    binance, bybit = await asyncio.gather(_binance_price(sym), _bybit_price(sym))
    cexes = [c for c in (binance, bybit) if c]
    anchor = cexes[0]["price_usd"] if cexes else None
    dex = await _dex_price(sym, anchor)

    if dex is None or not cexes:
        raise HTTPException(status_code=502,
                            detail="Need at least one DEX and one CEX price; a side was unreachable. Not charged.")

    # reference CEX = first available; spread computed against it
    cex = cexes[0]
    dex_p, cex_p = dex["price_usd"], cex["price_usd"]
    gross_pct = (dex_p - cex_p) / cex_p * 100
    total_fees = CEX_TAKER_FEE_PCT + DEX_SWAP_FEE_PCT
    net_pct = abs(gross_pct) - total_fees
    if dex_p > cex_p:
        direction = f"buy on {cex['venue']} (CEX), sell on DEX ({dex.get('dex')})"
    else:
        direction = f"buy on DEX ({dex.get('dex')}), sell on {cex['venue']} (CEX)"

    shaped = {
        "query": {"symbol": sym},
        "dex": dex,
        "cex": cex,
        "all_cex": cexes,
        "spread": {
            "gross_spread_pct": round(gross_pct, 4),
            "abs_gross_spread_pct": round(abs(gross_pct), 4),
            "assumed_fees_pct": {"cex_taker": CEX_TAKER_FEE_PCT, "dex_swap": DEX_SWAP_FEE_PCT, "total": total_fees},
            "net_spread_pct": round(net_pct, 4),
            "profitable": net_pct > 0,
            "direction": direction if net_pct > 0 else None,
        },
        "sources_ok": {"dexscreener": True, **{c["venue"]: True for c in cexes}},
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Net spread uses assumed taker/swap fees and ignores gas, slippage and withdrawal time. Not advice.",
    }
    _cache.set(sym, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/dex-cex-spread")
async def dex_cex_spread(
    symbol: str = Query("ETH", description="Coin ticker, e.g. 'ETH', 'WBTC', 'ARB'"),
) -> JSONResponse:
    """GET /crypto/dex-cex-spread — DEX vs CEX price gap with fee-adjusted net profit and execution direction."""
    return JSONResponse(content=await spread(symbol))


@router.get("/crypto/dex-cex-spread/health")
async def dex_cex_spread_health() -> JSONResponse:
    import asyncio
    binance, bybit = await asyncio.gather(_binance_price("ETH"), _bybit_price("ETH"))
    anchor = (binance or bybit or {}).get("price_usd")
    dex = await _dex_price("ETH", anchor)
    ok = dex is not None and (binance is not None or bybit is not None)
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "dex-cex-spread", "status": "ok" if ok else "degraded",
        "sources_reachable": {"dexscreener": dex is not None, "binance": binance is not None, "bybit": bybit is not None},
        "source": SOURCE, "cache_entries": len(_cache)})
