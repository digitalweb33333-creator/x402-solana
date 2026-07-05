"""Endpoint — New DEX pairs + instant safety (sniping).

Differentiator: does not return just a feed of new pairs, but EACH new pair
ENRICHED with an immediate safety check (GoPlus) — a sniping bot filters rugs
without a second call.

Sources (free, keyless):
- GeckoTerminal (api.geckoterminal.com) — new pools per network.
- GoPlus Security — honeypot/taxes/open-source of the base token (EVM chains).

"computed" tier $0.05. TTL 30 s (the new-pairs feed moves fast).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache, client, get_json, utc_now

router = APIRouter()

SOURCE = "GeckoTerminal (new pools) + GoPlus Security (token safety)"
_cache = TTLCache(30)

# user network -> (GeckoTerminal id, GoPlus EVM chain_id or None for non-EVM)
NETWORKS = {
    "base": ("base", "8453"), "ethereum": ("eth", "1"), "eth": ("eth", "1"),
    "bsc": ("bsc", "56"), "bnb": ("bsc", "56"), "polygon": ("polygon_pos", "137"),
    "arbitrum": ("arbitrum", "42161"), "arb": ("arbitrum", "42161"),
    "optimism": ("optimism", "10"), "avalanche": ("avax", "43114"),
    "solana": ("solana", None),  # non-EVM : pas de safety GoPlus EVM
}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _quick_safety(chain_id: str, addr: str) -> dict | None:
    """GoPlus mini-check (1 call): honeypot, taxes, open-source -> mini-score."""
    c = await client("goplus", timeout=10.0)
    data, err = await get_json(c, f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
                               params={"contract_addresses": addr})
    if err:
        return None
    res = (data or {}).get("result", {}) or {}
    gp = res.get(addr.lower()) or (next(iter(res.values()), None) if res else None)
    if not gp:
        return None
    honeypot = str(gp.get("is_honeypot")) == "1"
    buy_tax = _num(gp.get("buy_tax"))
    sell_tax = _num(gp.get("sell_tax"))
    open_source = str(gp.get("is_open_source")) == "1"
    # GoPlus often has NO history on a token a few minutes old: absence of data
    # != "safe". We cap and mark "limited data" in that case.
    hc = gp.get("holder_count")
    sparse = hc in (None, "", "0", 0) or len(gp) <= 3
    score = 100
    flags = []
    if honeypot:
        score = 0; flags.append("honeypot")
    if sell_tax is not None and sell_tax > 0.1:
        score -= 30; flags.append(f"sell tax {sell_tax*100:.0f}%")
    if not open_source:
        score -= 15; flags.append("not verified")
    if str(gp.get("is_mintable")) == "1":
        score -= 10; flags.append("mintable")
    if sparse and not honeypot:
        score = min(score, 55); flags.append("brand-new: limited on-chain history, treat as high risk")
    score = max(0, score)
    return {"safety_score": score, "honeypot": honeypot,
            "buy_tax_pct": buy_tax * 100 if buy_tax is not None else None,
            "sell_tax_pct": sell_tax * 100 if sell_tax is not None else None,
            "open_source": open_source, "data_limited": sparse, "flags": flags}


async def list_new_pairs(chain: str, limit: int, safety: bool) -> dict[str, Any]:
    ch = (chain or "base").strip().lower()
    net = NETWORKS.get(ch)
    if not net:
        raise HTTPException(status_code=400, detail=f"Unsupported 'chain'. Use one of: {', '.join(sorted(NETWORKS))}.")
    if not (1 <= limit <= 15):
        raise HTTPException(status_code=400, detail="'limit' must be in [1, 15].")
    gt_net, goplus_chain = net

    key = f"{ch}|{limit}|{safety}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    c = await client("geckoterminal", timeout=12.0)
    data, err = await get_json(c, f"https://api.geckoterminal.com/api/v2/networks/{gt_net}/new_pools",
                               params={"page": 1})
    if err:
        raise HTTPException(status_code=502, detail=f"New-pairs source unreachable ({err}); not charged.")
    pools = (data or {}).get("data") or []
    if not pools:
        raise HTTPException(status_code=502, detail="No new pools returned; not charged.")

    pairs = []
    for p in pools[:limit]:
        a = p.get("attributes") or {}
        base_id = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        token_addr = base_id.split("_", 1)[1] if "_" in base_id else None
        pairs.append({
            "pool_name": a.get("name"),
            "pool_address": a.get("address"),
            "base_token_address": token_addr,
            "price_usd": _num(a.get("base_token_price_usd")),
            "created_at": a.get("pool_created_at"),
            "liquidity_usd": _num(a.get("reserve_in_usd")),
            "fdv_usd": _num(a.get("fdv_usd")),
            "volume_24h_usd": _num((a.get("volume_usd") or {}).get("h24")),
        })

    # Differentiator: immediate per-pair safety (EVM only)
    if safety and goplus_chain:
        targets = [(i, pr["base_token_address"]) for i, pr in enumerate(pairs) if pr["base_token_address"]]
        safeties = await asyncio.gather(*[_quick_safety(goplus_chain, addr) for _, addr in targets])
        for (i, _), s in zip(targets, safeties):
            pairs[i]["safety"] = s
    elif safety and not goplus_chain:
        for pr in pairs:
            pr["safety"] = {"note": "GoPlus EVM safety not available for this non-EVM chain"}

    shaped = {
        "query": {"chain": ch, "limit": limit, "safety": safety},
        "count": len(pairs),
        "pairs": pairs,
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "New pools are high-risk by nature; safety score is heuristic. Not financial advice.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/new-pairs")
async def new_pairs(
    chain: str = Query("base", description="Chain: base | ethereum | bsc | polygon | arbitrum | solana ..."),
    limit: int = Query(5, description="Max new pairs [1-15], e.g. 5"),
    safety: bool = Query(True, description="Bundle a GoPlus safety check per pair (EVM only)"),
) -> JSONResponse:
    """GET /crypto/new-pairs — newest DEX pools with an instant per-pair safety check bundled in one call."""
    return JSONResponse(content=await list_new_pairs(chain, limit, safety))


@router.get("/crypto/new-pairs/health")
async def new_pairs_health() -> JSONResponse:
    c = await client("geckoterminal")
    data, err = await get_json(c, "https://api.geckoterminal.com/api/v2/networks/base/new_pools", params={"page": 1})
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "new-pairs", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
