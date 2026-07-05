"""Endpoint 6 — Pre-Trade Decision Bundle (Solana) : LA décision pré-trade en 1 appel.

Fusionne 4 modules (chacun avec sous-score + reasons[]) en UN verdict
BUY-SAFE/CAUTION/AVOID : (1) sécurité token, (2) PROFONDEUR DE LIQUIDITÉ EXÉCUTABLE
(slippage estimé à la taille — ce que les concurrents simulent par un simple
"LP locked oui/non"), (3) historique du déployeur (le contrôleur a-t-il encore le
pouvoir / est-il neuf ?), (4) concentration des holders.

Angle (cf benchmark) : niche la plus contestée (SolSignal, RugGuard). On ne ship
QUE si on sur-résout sur les 2 choses qu'ils truquent : profondeur exécutable +
lignée du déployeur. Réutilise le moteur de l'endpoint 5.

5 règles : verdict en haut, confidence + reasons[], modules avec sous-scores,
data_freshness, codes d'erreur, ABSTAIN si données insuffisantes.

Source : RPC Solana + DexScreener (gratuit). Tier $0.05. TTL 3 min.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache
from app.sources.solana_rpc import (
    KNOWN_SAFE, get_recent_signatures, num, valid_mint,
)
from app.routers.solana_token_safety import token_security_module
from app.verdict import age_seconds, clamp01, freshness, now_iso, reason
import time

router = APIRouter()

SOURCES = ["Solana RPC", "DexScreener"]
_cache = TTLCache(180)
# Tailles de trade pour la simulation de slippage (USD).
TRADE_SIZES = [100, 1000, 10000]


def _depth_module(market: dict[str, Any]) -> dict[str, Any]:
    """Profondeur exécutable : slippage estimé par taille sur un AMM constant-product."""
    liq = num(market.get("liquidity_usd"))
    if not liq or liq <= 0:
        return {"status": "limited", "sub_score": 30, "reasons": [
            reason("NO_LIQUIDITY_DATA", "No liquidity figure — cannot estimate executable depth", 0.5)],
            "slippage_estimates": None}
    one_side = liq / 2.0  # réserve approx du côté quote
    estimates = []
    for t in TRADE_SIZES:
        # CPMM : impact ≈ t / (reserve + t) ; borne basse réaliste de l'exécutable.
        impact = t / (one_side + t)
        estimates.append({"trade_usd": t, "est_price_impact_pct": round(impact * 100, 2)})
    impact_1k = next(e["est_price_impact_pct"] for e in estimates if e["trade_usd"] == 1000)
    reasons = []
    if impact_1k < 1:
        sub = 95; reasons.append(reason("DEEP_LIQUIDITY", f"~{impact_1k:.1f}% impact on a $1k trade", -0.5))
    elif impact_1k < 5:
        sub = 70; reasons.append(reason("MODERATE_DEPTH", f"~{impact_1k:.1f}% impact on a $1k trade", 0.2))
    elif impact_1k < 15:
        sub = 45; reasons.append(reason("SHALLOW_DEPTH", f"~{impact_1k:.0f}% impact on a $1k trade", 0.5))
    else:
        sub = 20; reasons.append(reason("VERY_SHALLOW_DEPTH", f"~{impact_1k:.0f}% impact on a $1k trade — barely executable", 0.8))
    return {"status": "ok", "sub_score": sub, "liquidity_usd": liq,
            "slippage_estimates": estimates, "reasons": reasons,
            "note": "Estimated from pooled liquidity via a constant-product model; real slippage depends on routing."}


async def _deployer_module(mint: str, mint_info: dict | None) -> dict[str, Any]:
    """Lignée/pouvoir du déployeur (best-effort, honnête sur ses limites)."""
    controller = (mint_info or {}).get("mint_authority") or (mint_info or {}).get("freeze_authority")
    reasons = []
    if not controller:
        return {"status": "renounced", "sub_score": 80, "controller": None,
                "reasons": [reason("AUTHORITIES_RENOUNCED", "Mint & freeze authority renounced — deployer retains no control", -0.4)],
                "note": "Deployer no longer controls the mint; on-chain rug-lineage of the original creator not cheaply resolvable on public RPC."}
    sigs, _ = await get_recent_signatures(controller, limit=1000)
    times = [s.get("blockTime") for s in (sigs or []) if s.get("blockTime")]
    first = min(times) if times else None
    age_days = (age_seconds(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(first))) or 0) / 86400.0 if first else None
    sub = 60
    if (mint_info or {}).get("mint_authority"):
        sub -= 25; reasons.append(reason("DEPLOYER_RETAINS_MINT", "Controller still holds mint authority (re-mint / dilution power)", 0.6))
    if age_days is not None and age_days < 3:
        sub -= 15; reasons.append(reason("FRESH_CONTROLLER", f"Controller wallet first seen ~{age_days:.1f}d ago", 0.4))
    elif age_days is not None and age_days > 90:
        sub += 10; reasons.append(reason("AGED_CONTROLLER", f"Controller active for ~{age_days:.0f}d", -0.2))
    return {"status": "ok", "sub_score": max(0, min(100, sub)), "controller": controller,
            "controller_min_age_days": round(age_days, 1) if age_days is not None else None,
            "controller_tx_seen": len(times), "reasons": reasons,
            "note": "Controller = current mint/freeze authority; 'fresh + retains mint' is the rug-lineage red flag."}


def _conc_module(concentration: dict | None) -> dict[str, Any]:
    if not concentration:
        return {"status": "limited", "sub_score": 50, "reasons": [
            reason("NO_HOLDER_DATA", "Holder distribution unavailable", 0.3)]}
    top1 = concentration.get("top1_share") or 0.0
    reasons = []
    if top1 > 0.6:
        sub = 35; reasons.append(reason("CONCENTRATED", f"Top account {top1*100:.0f}% (may be LP — verify)", 0.4))
    elif top1 > 0.3:
        sub = 60; reasons.append(reason("MODERATE_CONCENTRATION", f"Top account {top1*100:.0f}%", 0.2))
    else:
        sub = 85; reasons.append(reason("DISTRIBUTED", f"Top account {top1*100:.0f}%", -0.3))
    return {"status": "ok", "sub_score": sub, "top1_share": top1,
            "top5_share": concentration.get("top5_share"), "reasons": reasons,
            "note": concentration.get("note")}


async def decide(mint: str) -> dict[str, Any]:
    m = (mint or "").strip()
    if not valid_mint(m):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MINT",
                            "message": "'mint' must be a base58 Solana mint address (32 bytes)."})
    if m in KNOWN_SAFE:
        return {"verdict": "BUY-SAFE", "confidence": 1.0, "composite_score": 100,
                "reasons": [reason("WHITELISTED_BLUECHIP", f"{KNOWN_SAFE[m]} is a known major asset", -1.0)],
                "query": {"mint": m}, "modules": {"false_positive_guard": True},
                "data_freshness": freshness(now_iso(), deterministic=True, sources=SOURCES, extra={"whitelist": True}),
                "error": None, "timestamp": now_iso(),
                "disclaimer": "Automated pre-trade decision, not financial advice. Always DYOR."}

    cached = _cache.get(m)
    if cached is not None:
        return {**cached, "cached": True}

    sec = await token_security_module(m, deep=True)
    if sec["status"] != "ok":
        raise HTTPException(status_code=502, detail={"code": "TOKEN_NOT_FOUND",
                            "message": f"Solana token data unavailable ({sec.get('detail')}); not charged."})

    depth = _depth_module(sec["market"])
    deployer = await _deployer_module(m, sec.get("mint_info"))
    conc = _conc_module(sec.get("concentration"))

    modules = {
        "token_security": {"sub_score": sec["score"], "status": "ok",
                           "static_flags": sec["static_flags"], "behavioral_flags": sec["behavioral_flags"],
                           "behavioral_status": sec["behavioral_status"]},
        "liquidity_depth": depth,
        "deployer_history": deployer,
        "holder_concentration": conc,
    }
    # Fusion pondérée : sécurité 40 %, profondeur 25 %, déployeur 20 %, concentration 15 %.
    weights = {"token_security": 0.40, "liquidity_depth": 0.25, "deployer_history": 0.20, "holder_concentration": 0.15}
    composite = sum(modules[k]["sub_score"] * w for k, w in weights.items())
    composite = round(max(0.0, min(100.0, composite)))

    freeze = bool((sec.get("mint_info") or {}).get("freeze_authority"))
    # Kill-switches : un module critique force AVOID quel que soit le composite.
    kill = freeze or depth["sub_score"] <= 20 or sec["score"] < 35
    if kill or composite < 45:
        verdict = "AVOID"
    elif composite < 70:
        verdict = "CAUTION"
    else:
        verdict = "BUY-SAFE"

    all_reasons = []
    for k in modules:
        all_reasons.extend(modules[k].get("reasons", []) if isinstance(modules[k].get("reasons"), list) else [])
    if freeze:
        all_reasons.insert(0, reason("FREEZE_AUTHORITY_KILL", "Freeze authority active — forced AVOID", 1.0))

    confidence = clamp01(0.55 + 0.15 * sum(1 for k in modules if modules[k].get("status") == "ok") / 4
                         + (0.1 if sec["behavioral_status"] == "ok" else -0.1))

    shaped = {
        "verdict": verdict, "confidence": round(confidence, 3), "composite_score": composite,
        "reasons": all_reasons[:12],
        "query": {"mint": m},
        "module_weights": weights,
        "modules": modules,
        "market": sec["market"],
        "data_freshness": freshness(now_iso(), deterministic=False, sources=SOURCES,
                                    extra={"kill_switch": bool(kill), "behavioral_status": sec["behavioral_status"]}),
        "error": None, "timestamp": now_iso(),
        "disclaimer": "All-in-one pre-trade decision (security + executable depth + deployer + concentration). "
                      "Heuristic, not financial advice. Always DYOR.",
    }
    _cache.set(m, shaped)
    return {**shaped, "cached": False}


@router.get("/solana/pre-trade")
async def solana_pre_trade(
    mint: str = Query(..., description="SPL token mint address (base58) to evaluate before buying"),
) -> JSONResponse:
    """GET /solana/pre-trade — one-call BUY-SAFE/CAUTION/AVOID fusing security + executable liquidity depth + deployer + concentration."""
    return JSONResponse(content=await decide(mint))


@router.get("/solana/pre-trade/health")
async def solana_pre_trade_health() -> JSONResponse:
    sec = await token_security_module("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", deep=False)
    ok = sec["status"] == "ok"
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "solana-pre-trade", "status": "ok" if ok else "degraded",
        "upstream": {"reachable": ok}, "cache_entries": len(_cache)})
