"""Endpoint — Token safety / rug check (pre-trade).

Differentiator: bundles 3 token security checks in one call (GoPlus + Honeypot.is
+ DexScreener liquidity) into a normalized 0-100 score with an LLM-ready verdict.
Replaces 3 calls + the agent's own scoring logic.

Sources (all free, keyless, pay-per-call/free):
- GoPlus Security API (api.gopluslabs.io) — contract security (honeypot, taxes, holders, LP).
- Honeypot.is (api.honeypot.is) — honeypot simulation + real taxes.
- DexScreener (api.dexscreener.com) — liquidity, price, volume.

"computed" tier $0.05. TTL 5 min (security data is stable, liquidity moves).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache, client, get_json, utc_now

router = APIRouter()

CHAIN_IDS = {"ethereum": "1", "eth": "1", "base": "8453", "bsc": "56", "bnb": "56",
             "polygon": "137", "arbitrum": "42161", "arb": "42161", "optimism": "10",
             "avalanche": "43114", "avax": "43114"}
SOURCE = "GoPlus Security + Honeypot.is + DexScreener"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(300)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _goplus(chain_id: str, addr: str) -> tuple[dict | None, str | None]:
    c = await client("goplus", timeout=12.0)
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
    data, err = await get_json(c, url, params={"contract_addresses": addr})
    if err:
        return None, err
    res = (data or {}).get("result", {}) or {}
    # the key is the lowercased address
    entry = res.get(addr.lower()) or (next(iter(res.values()), None) if res else None)
    return entry, None if entry else "empty"


async def _honeypot(chain_id: str, addr: str) -> tuple[dict | None, str | None]:
    c = await client("honeypot", timeout=12.0)
    url = "https://api.honeypot.is/v2/IsHoneypot"
    data, err = await get_json(c, url, params={"address": addr, "chainID": chain_id})
    return (data, None) if not err else (None, err)


async def _dexscreener(addr: str) -> tuple[dict | None, str | None]:
    c = await client("dexscreener", timeout=12.0)
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
    data, err = await get_json(c, url)
    if err:
        return None, err
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return None, "no_pairs"
    # best pair = max USD liquidity
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
    return best, None


def _compute_score(gp: dict | None, hp: dict | None, dex: dict | None) -> dict[str, Any]:
    """Score 0-100 (100 = safe). Returns score, rating, flags, verdict."""
    score = 100.0
    flags: list[str] = []
    honeypot = False

    # --- Honeypot (kill switch) ---
    if gp and str(gp.get("is_honeypot")) == "1":
        honeypot = True
    if hp and isinstance(hp.get("honeypotResult"), dict) and hp["honeypotResult"].get("isHoneypot"):
        honeypot = True
    if honeypot:
        flags.append("HONEYPOT: cannot sell")

    # --- Taxes (prefer Honeypot.is simulation, else GoPlus) ---
    buy_tax = sell_tax = None
    if hp and isinstance(hp.get("simulationResult"), dict):
        buy_tax = _num(hp["simulationResult"].get("buyTax"))
        sell_tax = _num(hp["simulationResult"].get("sellTax"))
    if buy_tax is None and gp:
        bt = _num(gp.get("buy_tax"))
        buy_tax = bt * 100 if bt is not None else None
    if sell_tax is None and gp:
        st = _num(gp.get("sell_tax"))
        sell_tax = st * 100 if st is not None else None
    if sell_tax is not None:
        if sell_tax >= 50:
            score -= 60; flags.append(f"sell tax {sell_tax:.0f}% (extreme)")
        elif sell_tax >= 10:
            score -= 30; flags.append(f"sell tax {sell_tax:.0f}% (high)")
    if buy_tax is not None and buy_tax >= 10:
        score -= 15; flags.append(f"buy tax {buy_tax:.0f}%")

    # --- Contrat (GoPlus) ---
    if gp:
        if str(gp.get("is_open_source")) == "0":
            score -= 15; flags.append("contract not verified/open-source")
        if str(gp.get("is_mintable")) == "1":
            score -= 10; flags.append("mintable supply")
        if str(gp.get("hidden_owner")) == "1":
            score -= 15; flags.append("hidden owner")
        if str(gp.get("can_take_back_ownership")) == "1":
            score -= 15; flags.append("ownership can be reclaimed")
        if str(gp.get("selfdestruct")) == "1":
            score -= 20; flags.append("self-destruct present")
        if str(gp.get("is_blacklisted")) == "1" or str(gp.get("cannot_sell_all")) == "1":
            score -= 15; flags.append("blacklist / cannot-sell-all")
        if str(gp.get("trading_cooldown")) == "1" or str(gp.get("slippage_modifiable")) == "1":
            score -= 5; flags.append("modifiable trading restrictions")
        # concentration : top holder
        holders = gp.get("holders") or []
        if holders:
            top = _num(holders[0].get("percent"))
            if top is not None and top > 0.5:
                score -= 15; flags.append(f"top holder owns {top*100:.0f}%")
        # LP locked/burned
        lp = gp.get("lp_holders") or []
        locked = any(str(h.get("is_locked")) == "1" for h in lp) or any(
            (h.get("address") or "").lower() in (
                "0x000000000000000000000000000000000000dead",
                "0x0000000000000000000000000000000000000000") for h in lp)
        if lp and not locked:
            score -= 10; flags.append("LP not locked/burned")

    # --- Liquidity (DexScreener) ---
    liq = None
    if dex:
        liq = _num((dex.get("liquidity") or {}).get("usd"))
        if liq is not None and liq < 10000:
            score -= 15; flags.append(f"low liquidity ${liq:,.0f}")

    if honeypot:
        score = 0.0
    score = max(0.0, min(100.0, score))

    if honeypot:
        rating, verdict = "critical", "DO NOT BUY — honeypot: token cannot be sold."
    elif score >= 80:
        rating, verdict = "safe", "Low risk on automated checks. Always DYOR."
    elif score >= 50:
        rating, verdict = "caution", "Moderate risk — review the flags before trading."
    else:
        rating, verdict = "high_risk", "High risk — multiple red flags detected."
    return {"safety_score": round(score), "rating": rating, "verdict": verdict,
            "honeypot": honeypot, "buy_tax_pct": buy_tax, "sell_tax_pct": sell_tax,
            "flags": flags}


async def assess_token(token: str, chain: str) -> dict[str, Any]:
    addr = (token or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail="'token' must be a contract address (0x + 40 hex).")
    chain_id = CHAIN_IDS.get((chain or "base").strip().lower())
    if not chain_id:
        raise HTTPException(status_code=400, detail=f"Unsupported 'chain'. Use one of: {', '.join(sorted(set(CHAIN_IDS)))}.")

    key = f"{chain_id}|{addr.lower()}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    gp_r, hp_r, dex_r = await asyncio.gather(
        _goplus(chain_id, addr), _honeypot(chain_id, addr), _dexscreener(addr))
    gp, gp_err = gp_r
    hp, hp_err = hp_r
    dex, dex_err = dex_r

    # All sources silent -> 502 (no charge; middleware does not settle on >=400)
    if gp is None and hp is None and dex is None:
        raise HTTPException(status_code=502, detail="All security sources unreachable / token not found; not charged.")

    score = _compute_score(gp, hp, dex)
    pair_info = None
    if dex:
        pair_info = {
            "price_usd": _num(dex.get("priceUsd")),
            "liquidity_usd": _num((dex.get("liquidity") or {}).get("usd")),
            "volume_24h_usd": _num((dex.get("volume") or {}).get("h24")),
            "dex": dex.get("dexId"),
            "pair_url": dex.get("url"),
            "symbol": (dex.get("baseToken") or {}).get("symbol"),
        }
    shaped = {
        "query": {"token": addr, "chain": chain.lower() if chain else "base"},
        **score,
        "market": pair_info,
        "holder_count": (gp or {}).get("holder_count"),
        "sources_ok": {"goplus": gp is not None, "honeypot_is": hp is not None, "dexscreener": dex is not None},
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Automated heuristic safety check, not financial advice. Always do your own research.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/token-safety")
async def token_safety(
    token: str = Query(..., description="Token contract address, e.g. '0x...' (40 hex)"),
    chain: str = Query("base", description="Chain: base | ethereum | bsc | polygon | arbitrum | optimism | avalanche"),
) -> JSONResponse:
    """GET /crypto/token-safety — honeypot + tax + holders + LP + liquidity, bundled into a 0-100 safety score."""
    return JSONResponse(content=await assess_token(token, chain))


@router.get("/crypto/token-safety/health")
async def token_safety_health() -> JSONResponse:
    c = await client("goplus")
    data, err = await get_json(c, "https://api.gopluslabs.io/api/v1/supported_chains")
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "token-safety", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
