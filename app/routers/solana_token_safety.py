"""Endpoint 5 — Token Safety Pro (Solana-first) : statique + COMPORTEMENTAL + anti-faux-positif.

Sécurité token pré-trade pour un agent. Différenciateur (cf étude académique
SolRugDetector + benchmark) : les concurrents Solana font du STATIQUE (snapshot des
autorités/holders) → trop large, flaggent même USDC comme rug. On ajoute (a) une
couche COMPORTEMENTALE (activité/vélocité, adéquation liquidité, action de prix) et
(b) un FALSE-POSITIVE GUARD explicite (whitelist USDC/USDT/SOL… jamais flaggés).

5 règles : verdict SAFE/RISKY/CRITICAL (+ABSTAIN) en haut, score 0-100, confidence +
reasons[], static_flags[]/behavioral_flags[], data_freshness/deterministic/sources,
codes d'erreur, ABSTAIN si trop récent pour le comportemental.

Sources : RPC Solana public (mint, holders, signatures) + DexScreener (liquidité,
âge du pool, prix). Gratuit, sans clé. Tier $0.01 quick / $0.05 deep. TTL 3 min.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache
from app.sources.solana_rpc import (
    KNOWN_SAFE, dexscreener_token, get_largest_accounts, get_mint_info,
    get_recent_signatures, num, valid_mint,
)
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = ["Solana RPC (mint, largest accounts, signatures)", "DexScreener (liquidity, pool age, price)"]
_cache = TTLCache(180)
MIN_BEHAVIORAL_AGE_H = 24.0  # plus jeune : comportemental ABSTAIN (pas assez d'historique)


def _pool_age_hours(dex: dict | None) -> float | None:
    if not dex:
        return None
    created_ms = num(dex.get("pairCreatedAt"))
    if not created_ms:
        return None
    return max(0.0, (time.time() - created_ms / 1000.0) / 3600.0)


async def token_security_module(mint: str, deep: bool) -> dict[str, Any]:
    """Cœur réutilisable (aussi appelé par le Pre-Trade Bundle, endpoint 6)."""
    mi, mi_err = await get_mint_info(mint)
    dex, dex_err = await dexscreener_token(mint)
    if mi is None and dex is None:
        return {"status": "unavailable", "error_code": "TOKEN_NOT_FOUND",
                "detail": f"mint:{mi_err} / dex:{dex_err}", "score": None}

    static_flags: list[dict] = []
    behavioral_flags: list[dict] = []
    reasons: list[dict] = []
    score = 100.0

    # --- STATIQUE (autorités, supply, holders) ---
    if mi:
        if mi.get("mint_authority"):
            score -= 25; static_flags.append({"code": "MINT_AUTHORITY_ACTIVE",
                "label": "Mint authority not renounced — supply can be inflated", "weight": 0.6})
        if mi.get("freeze_authority"):
            score -= 30; static_flags.append({"code": "FREEZE_AUTHORITY_ACTIVE",
                "label": "Freeze authority active — issuer can freeze your tokens (sell-block risk)", "weight": 0.8})
        if mi.get("is_token_2022"):
            score -= 8; static_flags.append({"code": "TOKEN_2022",
                "label": "Token-2022 program — verify transfer-fee / transfer-hook extensions", "weight": 0.3})

    # Concentration des holders (avec mise en garde : un top-holder peut être le pool AMM).
    concentration = None
    if deep or mi:
        largest, _ = await get_largest_accounts(mint)
        supply_raw = num((mi or {}).get("supply"))
        if largest and supply_raw and supply_raw > 0:
            amounts = sorted((num(a.get("amount")) or 0.0) for a in largest)
            amounts.reverse()
            top1 = amounts[0] / supply_raw if amounts else 0.0
            top5 = sum(amounts[:5]) / supply_raw if amounts else 0.0
            concentration = {"top1_share": round(top1, 4), "top5_share": round(top5, 4),
                             "note": "Largest account may be an AMM/LP pool, not a malicious whale."}
            if top1 > 0.5:
                score -= 10; static_flags.append({"code": "HIGH_TOP_HOLDER",
                    "label": f"Top account holds {top1*100:.0f}% (may be LP — verify)", "weight": 0.3})

    # --- LIQUIDITÉ / MARCHÉ ---
    liquidity_usd = fdv = vol24 = price_change_h24 = None
    if dex:
        liquidity_usd = num((dex.get("liquidity") or {}).get("usd"))
        fdv = num(dex.get("fdv"))
        vol24 = num((dex.get("volume") or {}).get("h24"))
        price_change_h24 = num((dex.get("priceChange") or {}).get("h24"))
        if liquidity_usd is not None and liquidity_usd < 5000:
            score -= 20; static_flags.append({"code": "LOW_LIQUIDITY",
                "label": f"Thin liquidity ${liquidity_usd:,.0f} — easy to rug/dump", "weight": 0.6})

    # --- COMPORTEMENTAL (le différenciateur) ---
    age_h = _pool_age_hours(dex)
    behavioral_status = "ok"
    if age_h is not None and age_h < MIN_BEHAVIORAL_AGE_H:
        behavioral_status = "abstain_too_new"
        reasons.append(reason("TOO_NEW_FOR_BEHAVIORAL",
                              f"Pool only {age_h:.1f}h old — behavioral history insufficient", 0.2))
    else:
        # adéquation liquidité vs valorisation
        if liquidity_usd and fdv and fdv > 0 and (liquidity_usd / fdv) < 0.02:
            score -= 15; behavioral_flags.append({"code": "LIQUIDITY_TO_FDV_THIN",
                "label": f"Liquidity is {liquidity_usd/fdv*100:.1f}% of FDV — top-heavy valuation", "weight": 0.5})
        # churn : volume très supérieur à la liquidité
        if liquidity_usd and vol24 and liquidity_usd > 0 and (vol24 / liquidity_usd) > 15:
            score -= 8; behavioral_flags.append({"code": "HIGH_CHURN",
                "label": f"24h volume is {vol24/liquidity_usd:.0f}x liquidity — abnormal churn", "weight": 0.35})
        # dump récent
        if price_change_h24 is not None and price_change_h24 < -50:
            score -= 12; behavioral_flags.append({"code": "RECENT_DUMP",
                "label": f"Price {price_change_h24:.0f}% in 24h — active dump", "weight": 0.5})
        # vélocité de transactions sur le mint
        sigs, _ = await get_recent_signatures(mint, limit=100 if not deep else 200)
        if sigs:
            times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
            if len(times) >= 2:
                span_min = max(1.0, (max(times) - min(times)) / 60.0)
                tx_per_min = len(times) / span_min
                if tx_per_min > 30 and (liquidity_usd or 0) < 20000:
                    score -= 6; behavioral_flags.append({"code": "VELOCITY_SPIKE",
                        "label": f"{tx_per_min:.0f} tx/min on a thin pool — possible bot pump", "weight": 0.3})

    score = max(0.0, min(100.0, score))
    sources_ok = {"solana_rpc": mi is not None, "dexscreener": dex is not None}
    market = {"liquidity_usd": liquidity_usd, "fdv_usd": fdv, "volume_24h_usd": vol24,
              "price_change_24h_pct": price_change_h24, "pool_age_hours": round(age_h, 1) if age_h else None,
              "symbol": (dex.get("baseToken") or {}).get("symbol") if dex else None,
              "dex": dex.get("dexId") if dex else None}
    return {"status": "ok", "score": round(score), "static_flags": static_flags,
            "behavioral_flags": behavioral_flags, "behavioral_status": behavioral_status,
            "reasons": reasons, "concentration": concentration, "market": market,
            "mint_info": {k: mi.get(k) for k in ("mint_authority", "freeze_authority", "decimals", "is_token_2022")} if mi else None,
            "sources_ok": sources_ok,
            "as_of": now_iso()}


async def assess(mint: str, deep: bool) -> dict[str, Any]:
    m = (mint or "").strip()
    if not valid_mint(m):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MINT",
                            "message": "'mint' must be a base58 Solana mint address (32 bytes)."})

    # --- FALSE-POSITIVE GUARD : blue-chips jamais flaggés ---
    if m in KNOWN_SAFE:
        return {
            "verdict": "SAFE", "confidence": 1.0, "score": 100,
            "reasons": [reason("WHITELISTED_BLUECHIP", f"{KNOWN_SAFE[m]} is a known major asset (false-positive guard)", -1.0)],
            "query": {"mint": m, "deep": deep},
            "static_flags": [], "behavioral_flags": [],
            "false_positive_guard": {"triggered": True, "asset": KNOWN_SAFE[m],
                                     "note": "Whitelisted major asset — never flagged as a rug (the bug that discredits static-only tools)."},
            "data_freshness": freshness(now_iso(), deterministic=True, sources=SOURCES, extra={"whitelist": True}),
            "error": None, "timestamp": now_iso(),
            "disclaimer": "Automated heuristic safety check, not financial advice. Always DYOR.",
        }

    key = f"{m}|{'deep' if deep else 'quick'}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    mod = await token_security_module(m, deep)
    if mod["status"] != "ok":
        raise HTTPException(status_code=502, detail={"code": mod["error_code"],
                            "message": f"Solana token data unavailable ({mod.get('detail')}); not charged."})

    score = mod["score"]
    risk = sum(f["weight"] for f in mod["static_flags"] + mod["behavioral_flags"])
    freeze = bool((mod.get("mint_info") or {}).get("freeze_authority"))
    if score < 45 or (freeze and score < 60):
        verdict = "CRITICAL"
    elif score < 75:
        verdict = "RISKY"
    else:
        verdict = "SAFE"
    # confidence : combien de sources + comportemental disponible
    confidence = clamp01(0.5 + 0.2 * sum(mod["sources_ok"].values())
                         + (0.1 if mod["behavioral_status"] == "ok" else -0.1))

    shaped = {
        "verdict": verdict, "confidence": round(confidence, 3), "score": score,
        "reasons": (mod["reasons"] or []) + [reason("COMPOSITE_SCORE", f"Composite safety score {score}/100", risk / 5)],
        "query": {"mint": m, "deep": deep},
        "static_flags": mod["static_flags"], "behavioral_flags": mod["behavioral_flags"],
        "behavioral_status": mod["behavioral_status"],
        "false_positive_guard": {"triggered": False,
                                 "note": "Not whitelisted; behavioral context applied to avoid static-only over-flagging."},
        "concentration": mod["concentration"], "market": mod["market"], "mint_info": mod["mint_info"],
        "sources_ok": mod["sources_ok"],
        "data_freshness": freshness(mod["market"].get("pool_age_hours") and now_iso() or now_iso(),
                                    deterministic=False, sources=SOURCES,
                                    extra={"behavioral_status": mod["behavioral_status"]}),
        "error": None, "timestamp": now_iso(),
        "disclaimer": "Automated heuristic safety check (static + behavioral), not financial advice. Always DYOR.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/solana/token-safety")
async def solana_token_safety(
    mint: str = Query(..., description="SPL token mint address (base58), e.g. 'EPjFW...Dt1v' (USDC)"),
    deep: bool = Query(False, description="Deeper behavioral + holder analysis (more RPC calls)"),
) -> JSONResponse:
    """GET /solana/token-safety — SPL token SAFE/RISKY/CRITICAL with static + behavioral flags and a blue-chip false-positive guard."""
    return JSONResponse(content=await assess(mint, deep))


@router.get("/solana/token-safety/health")
async def solana_token_safety_health() -> JSONResponse:
    mi, err = await get_mint_info("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")  # USDC
    ok = mi is not None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "solana-token-safety", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCES[0], "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
