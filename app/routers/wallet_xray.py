"""Endpoint — Wallet x-ray (portfolio + risk, 1 appel).

Differentiator: bundles native+token balances + USD VALUATION + token count +
OFAC SANCTION FLAG in one call. Where primitives (onesource) serve one field per
call, this serves the full wallet table, ready for an agent.

Sources (free, keyless):
- Blockscout (keyless API per chain) — native + ERC-20 balances.
- DexScreener — USD prices of held tokens.
- OFAC sanctioned-address list (raw GitHub 0xB10C/ofac-sanctioned-digital-currency-addresses).

"computed" tier $0.05. TTL 60 s.
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

SOURCE = "Blockscout + DexScreener + OFAC sanctioned-address list"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(60)
_ofac_cache = TTLCache(86400)  # 24h

BLOCKSCOUT = {
    "base": "https://base.blockscout.com", "ethereum": "https://eth.blockscout.com",
    "eth": "https://eth.blockscout.com", "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com", "arbitrum": "https://arbitrum.blockscout.com",
    "gnosis": "https://gnosis.blockscout.com",
}
# wrapped-native pour valoriser le solde natif via DexScreener
WNATIVE = {
    "base": ("0x4200000000000000000000000000000000000006", "ETH"),
    "ethereum": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "ETH"),
    "eth": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "ETH"),
    "optimism": ("0x4200000000000000000000000000000000000006", "ETH"),
    "arbitrum": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "ETH"),
    "polygon": ("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "MATIC"),
    "gnosis": ("0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d", "XDAI"),
}
OFAC_URL = ("https://raw.githubusercontent.com/0xB10C/"
            "ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_ETH.json")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _ofac_set() -> set[str]:
    cached = _ofac_cache.get("ofac")
    if cached is not None:
        return cached
    c = await client("github", timeout=12.0)
    data, err = await get_json(c, OFAC_URL)
    s = set(a.lower() for a in data) if isinstance(data, list) else set()
    if s:
        _ofac_cache.set("ofac", s)
    return s


async def _balances(host: str, addr: str) -> tuple[dict | None, list | None, str | None]:
    c = await client("blockscout", timeout=12.0)
    nat, e1 = await get_json(c, f"{host}/api", params={"module": "account", "action": "balance", "address": addr})
    toks, e2 = await get_json(c, f"{host}/api", params={"module": "account", "action": "tokenlist", "address": addr})
    if e1 and e2:
        return None, None, e1 or e2
    native_wei = _num((nat or {}).get("result"))
    token_list = (toks or {}).get("result") if isinstance((toks or {}).get("result"), list) else []
    return ({"native_wei": native_wei}, token_list, None)


_MIN_LIQ_USD = 20000.0  # anti-spam floor: a "valued" token must have real liquidity

async def _prices(addresses: list[str]) -> dict[str, float]:
    """USD price per address via DexScreener (batch <=30), only from sufficiently
    liquid pairs (filters spam tokens with manipulated prices)."""
    out: dict[str, float] = {}
    liq_seen: dict[str, float] = {}
    if not addresses:
        return out
    c = await client("dexscreener", timeout=12.0)
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        data, err = await get_json(c, f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}")
        if err:
            continue
        for p in (data or {}).get("pairs") or []:
            base = (p.get("baseToken") or {}).get("address", "").lower()
            price = _num(p.get("priceUsd"))
            liq = _num((p.get("liquidity") or {}).get("usd")) or 0.0
            if base and price is not None and liq >= _MIN_LIQ_USD:
                if liq > liq_seen.get(base, 0.0):  # keep the most liquid pair
                    out[base] = price
                    liq_seen[base] = liq
    return out


async def xray(address: str, chain: str) -> dict[str, Any]:
    addr = (address or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail="'address' must be a wallet address (0x + 40 hex).")
    ch = (chain or "base").strip().lower()
    host = BLOCKSCOUT.get(ch)
    if not host:
        raise HTTPException(status_code=400, detail=f"Unsupported 'chain'. Use one of: {', '.join(sorted(BLOCKSCOUT))}.")

    key = f"{ch}|{addr.lower()}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    (bal_r, ofac) = await asyncio.gather(_balances(host, addr), _ofac_set())
    native, tokens, err = bal_r
    if native is None and tokens is None:
        raise HTTPException(status_code=502, detail=f"Wallet data source unreachable ({err}); not charged.")
    tokens = tokens or []

    wnat_addr, nat_sym = WNATIVE.get(ch, (None, "ETH"))
    # perf cap: only price the first ~120 tokens (4 batches) to NEVER stall on
    # mega-wallets (vitalik = 7885 tokens). Best-effort valuation.
    PRICE_CAP = 120
    price_targets = [t.get("contractAddress", "").lower() for t in tokens if t.get("contractAddress")][:PRICE_CAP]
    if wnat_addr:
        price_targets.append(wnat_addr.lower())
    prices = await _prices(list(dict.fromkeys(price_targets)))
    valuation_capped = len(tokens) > PRICE_CAP

    # natif
    native_amount = (native.get("native_wei") or 0) / 1e18 if native else 0.0
    nat_price = prices.get((wnat_addr or "").lower())
    native_usd = native_amount * nat_price if (nat_price and native_amount) else None

    holdings = []
    total_usd = native_usd or 0.0
    for t in tokens:
        ca = (t.get("contractAddress") or "").lower()
        dec = int(_num(t.get("decimals")) or 18)
        raw = _num(t.get("balance"))
        amount = (raw / (10 ** dec)) if raw is not None else None
        price = prices.get(ca)
        usd = (amount * price) if (amount is not None and price is not None) else None
        if usd:
            total_usd += usd
        holdings.append({
            "symbol": t.get("symbol"), "name": t.get("name"), "contract": ca,
            "amount": amount, "price_usd": price, "value_usd": round(usd, 2) if usd else None,
            "type": t.get("type"),
        })
    # sort by descending USD value (None at the bottom)
    holdings.sort(key=lambda h: h["value_usd"] or -1, reverse=True)

    sanctioned = addr.lower() in ofac
    shaped = {
        "query": {"address": addr, "chain": ch},
        "sanction_check": {"ofac_listed": sanctioned, "source": "OFAC SDN crypto addresses",
                           "list_loaded": bool(ofac)},
        "native": {"symbol": nat_sym, "amount": native_amount, "price_usd": nat_price,
                   "value_usd": round(native_usd, 2) if native_usd else None},
        "portfolio": {
            "total_value_usd": round(total_usd, 2) if total_usd else 0.0,
            "token_count": len(holdings),
            "priced_token_count": sum(1 for h in holdings if h["value_usd"]),
            "valuation_capped": valuation_capped,
        },
        "holdings": holdings[:100],
        "risk_flags": (["OFAC sanctioned address"] if sanctioned else [])
                      + (["many unpriced tokens (possible spam/airdrops)"] if len(holdings) > 30 else [])
                      + (["top holding >40% of portfolio — verify its DEX price (possible spam-token mispricing)"]
                         if (holdings and total_usd and (holdings[0]["value_usd"] or 0) > 0.4 * total_usd) else []),
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Balances/valuations are best-effort from public indexers; OFAC list may lag. Not advice.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/crypto/wallet-xray")
async def wallet_xray(
    address: str = Query(..., description="Wallet address, e.g. '0x...' (40 hex)"),
    chain: str = Query("base", description="Chain: base | ethereum | optimism | arbitrum | polygon | gnosis"),
) -> JSONResponse:
    """GET /crypto/wallet-xray — native+token balances, USD valuation, token count and OFAC sanction flag in one call."""
    return JSONResponse(content=await xray(address, chain))


@router.get("/crypto/wallet-xray/health")
async def wallet_xray_health() -> JSONResponse:
    c = await client("blockscout")
    data, err = await get_json(c, "https://base.blockscout.com/api",
                               params={"module": "account", "action": "balance",
                                       "address": "0x0000000000000000000000000000000000000000"})
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "wallet-xray", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
