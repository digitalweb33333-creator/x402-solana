"""LOT 8 #1 — Pre-Trade Verdict ($0.05, flagship).

One-call GO / CAUTION / NO-GO pre-trade verdict fusing THREE internal checks that
today an agent has to call separately:
  (1) token safety  — /crypto/token-safety (EVM) or /solana/token-safety (Solana)
  (2) counterparty  — /compliance/wallet-screen (optional `wallet` param, OFAC/mixer)
  (3) market signal — /crypto/signal-fusion (directional funding/crowding/regime)

Whitespace (cf RAPPORT-BENCHMARK-12): competitors sell each of these separately;
nobody fuses safety + counterparty screening + signals into a single verdict + a
signed, offline-verifiable receipt.

100% internal composition — imports and calls the existing routers' callables (no
HTTP round-trip, no new source, no new key). Kill switches: honeypot or a BLOCKED
counterparty force NO-GO. Optional components (wallet, signal) degrade gracefully:
if a signal source is silent the verdict still ships, flagged in `components`.

"Never an error after payment": the token-safety leg is the product's core; if ALL
its sources are down it raises 502 (middleware does not settle → agent not charged).
The optional legs never fail the call.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.routers import signal_fusion, solana_token_safety, token_safety, wallet_screen
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES_LABEL = [
    "Internal /crypto/token-safety or /solana/token-safety (GoPlus + Honeypot.is + DexScreener / Solana RPC)",
    "Internal /compliance/wallet-screen (OFAC SDN crypto list + mixer labels)",
    "Internal /crypto/signal-fusion (Binance/Bybit/OKX/Hyperliquid perp + klines)",
]
_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_CHAINS = {"base", "ethereum", "eth", "bsc", "bnb", "polygon", "arbitrum", "arb", "optimism", "avalanche", "avax"}


def _validate(chain: str, token: str) -> str:
    """Validate the (chain, token) pair BEFORE any source call. Returns normalized chain."""
    c = (chain or "base").strip().lower()
    tok = (token or "").strip()
    if not tok:
        raise HTTPException(status_code=400, detail={"code": "TOKEN_REQUIRED", "message": "'token' (contract/mint address) is required."})
    if c == "solana" or c == "sol":
        if not _SOL_RE.match(tok):
            raise HTTPException(status_code=400, detail={"code": "BAD_SOLANA_MINT", "message": "For chain=solana, 'token' must be a base58 SPL mint address."})
        return "solana"
    if c in _EVM_CHAINS:
        if not _EVM_RE.match(tok):
            raise HTTPException(status_code=400, detail={"code": "BAD_EVM_ADDRESS", "message": "'token' must be an EVM contract address (0x + 40 hex) for this chain."})
        return c
    raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_CHAIN", "message": "'chain' must be one of: base, ethereum, eth, bsc, polygon, arbitrum, optimism, avalanche, solana."})


async def _safety_component(chain: str, token: str) -> dict[str, Any]:
    """Core leg. Returns a normalized safety component or raises (propagated -> no settle)."""
    if chain == "solana":
        data = await solana_token_safety.assess(token, deep=False)
        score = int(data.get("score", 50))          # 0-100, 100 = safe
        honeypot = data.get("verdict") == "CRITICAL"  # solana has no explicit honeypot flag
        symbol = (data.get("market") or {}).get("symbol")
        return {"available": True, "kind": "solana", "safety_score": score,
                "honeypot": honeypot, "rating": data.get("verdict"),
                "flags": [f["label"] for f in (data.get("static_flags") or []) + (data.get("behavioral_flags") or [])][:6],
                "symbol": symbol, "raw_verdict": data.get("verdict")}
    data = await token_safety.assess_token(token, chain)
    return {"available": True, "kind": "evm", "safety_score": int(data.get("safety_score", 50)),
            "honeypot": bool(data.get("honeypot")), "rating": data.get("rating"),
            "flags": list(data.get("flags") or [])[:6],
            "symbol": (data.get("market") or {}).get("symbol"),
            "buy_tax_pct": data.get("buy_tax_pct"), "sell_tax_pct": data.get("sell_tax_pct")}


async def _wallet_component(wallet: str | None) -> dict[str, Any]:
    """Optional counterparty screen. Never fails the call."""
    if not wallet or not wallet.strip():
        return {"available": False, "reason": "no wallet supplied"}
    try:
        data = await wallet_screen.screen(wallet.strip(), [])
        return {"available": True, "verdict": data.get("verdict"),
                "matched_lists": data.get("matched_lists") or [],
                "mixer_exposed": bool((data.get("mixer_exposure") or {}).get("exposed"))}
    except HTTPException as exc:
        return {"available": False, "reason": f"screen_error_{exc.status_code}"}
    except Exception as exc:  # best-effort, never propagate on the optional leg
        return {"available": False, "reason": type(exc).__name__}


async def _signal_component(symbol: str | None) -> dict[str, Any]:
    """Optional directional signal from perp venues, keyed by the token's ticker."""
    if not symbol or not str(symbol).strip():
        return {"available": False, "reason": "no ticker resolved from market data"}
    try:
        data = await signal_fusion.fuse(str(symbol).strip().upper(), "1h")
        return {"available": True, "symbol": str(symbol).upper(), "signal": data.get("verdict"),
                "confidence": data.get("confidence"), "fused_score": data.get("fused_score")}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__}


def _fuse(safety: dict, wallet: dict, signal: dict) -> dict[str, Any]:
    """Deterministic fusion → GO / CAUTION / NO-GO with signed reasons."""
    reasons: list[dict] = []
    score = safety["safety_score"]

    # --- Kill switches → NO-GO ---
    hard_block = False
    if safety.get("honeypot"):
        hard_block = True
        reasons.append(reason("HONEYPOT", "Token flagged as a honeypot / non-sellable — do not buy.", 1.0))
    if wallet.get("available") and wallet.get("verdict") == "BLOCK":
        hard_block = True
        reasons.append(reason("COUNTERPARTY_BLOCK", "Counterparty wallet is BLOCKED (sanctions/mixer match).", 1.0))
    if safety["safety_score"] < 40 and not hard_block:
        reasons.append(reason("SAFETY_CRITICAL", f"Token safety score {score}/100 is critical.", 0.8))

    # --- Contributing signals ---
    if not hard_block:
        if score >= 75:
            reasons.append(reason("SAFETY_OK", f"Token safety score {score}/100 (low risk on automated checks).", -0.5))
        elif score >= 40:
            reasons.append(reason("SAFETY_MODERATE", f"Token safety score {score}/100 (review flags).", 0.3))
    for f in safety.get("flags", [])[:3]:
        reasons.append(reason("SAFETY_FLAG", f"Token check: {f}", 0.2))

    if wallet.get("available"):
        wv = wallet.get("verdict")
        if wv == "WARN":
            reasons.append(reason("COUNTERPARTY_WARN", "Counterparty screen returned WARN.", 0.4))
        elif wv == "PASS":
            reasons.append(reason("COUNTERPARTY_PASS", "Counterparty screen PASS (no sanctions/mixer hit).", -0.3))

    signal_bearish = False
    if signal.get("available"):
        sv = signal.get("signal")
        if sv == "SHORT":
            signal_bearish = True
            reasons.append(reason("SIGNAL_BEARISH", "Cross-exchange signal is SHORT (bearish market context).", 0.3))
        elif sv == "LONG":
            reasons.append(reason("SIGNAL_BULLISH", "Cross-exchange signal is LONG (supportive market context).", -0.2))
        else:
            reasons.append(reason("SIGNAL_NEUTRAL", "Cross-exchange signal is NEUTRAL.", 0.0))

    # --- Verdict logic ---
    if hard_block:
        verdict = "NO-GO"
    elif score < 40 or (wallet.get("available") and wallet.get("verdict") == "WARN" and score < 60):
        verdict = "NO-GO" if score < 40 else "CAUTION"
    elif score >= 75 and (not wallet.get("available") or wallet.get("verdict") == "PASS") and not signal_bearish:
        verdict = "GO"
    else:
        verdict = "CAUTION"

    # Confidence: rises with how many components are available and how decisive.
    comps_ok = 1 + int(wallet.get("available", False)) + int(signal.get("available", False))
    base_conf = {"NO-GO": 0.9, "GO": 0.8, "CAUTION": 0.6}[verdict]
    confidence = round(clamp01(base_conf * (0.7 + 0.1 * comps_ok)), 3)
    return {"verdict": verdict, "confidence": confidence, "reasons": reasons}


async def evaluate(chain: str, token: str, wallet: str | None) -> dict[str, Any]:
    norm_chain = _validate(chain, token)
    safety = await _safety_component(norm_chain, token)           # may raise 400/502 (core leg)
    wallet_c = await _wallet_component(wallet)
    signal_c = await _signal_component(safety.get("symbol"))
    fused = _fuse(safety, wallet_c, signal_c)

    receipt = sign_receipt({
        "kind": "pre_trade_verdict",
        "chain": norm_chain,
        "token": token.strip(),
        "counterparty": (wallet or "").strip() or None,
        "verdict": fused["verdict"],
        "safety_score": safety["safety_score"],
        "counterparty_verdict": wallet_c.get("verdict") if wallet_c.get("available") else None,
        "signal": signal_c.get("signal") if signal_c.get("available") else None,
        "as_of": now_iso(),
    })

    return {
        "verdict": fused["verdict"],
        "confidence": fused["confidence"],
        "reasons": fused["reasons"],
        "query": {"chain": norm_chain, "token": token.strip(), "wallet": (wallet or "").strip() or None},
        "components": {
            "token_safety": safety,
            "counterparty_screen": wallet_c,
            "market_signal": signal_c,
        },
        "signed_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=False, sources=SOURCES_LABEL,
                                    extra={"components_available": [k for k, v in
                                           {"token_safety": True, "counterparty_screen": wallet_c.get("available"),
                                            "market_signal": signal_c.get("available")}.items() if v]}),
        "error": None,
        "timestamp": now_iso(),
        "disclaimer": "Fused pre-trade verdict composed from automated token-safety, sanctions-screening and market-signal checks. Heuristic, not financial advice. Always DYOR.",
        "cached": False,
    }


@router.get("/crypto/pre-trade-verdict")
async def pre_trade_verdict(
    token: str = Query(..., description="Token contract/mint address to evaluate before trading."),
    chain: str = Query("base", description="base | ethereum | bsc | polygon | arbitrum | optimism | avalanche | solana"),
    wallet: str | None = Query(None, description="Optional counterparty wallet to screen (OFAC/mixer)."),
) -> JSONResponse:
    """GET /crypto/pre-trade-verdict — one-call GO/CAUTION/NO-GO fusing token safety + counterparty screen + market signal + signed receipt."""
    return JSONResponse(content=await evaluate(chain, token, wallet))


@router.get("/crypto/pre-trade-verdict/health")
async def pre_trade_verdict_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "pre-trade-verdict", "status": "ok",
        "composition": ["/crypto/token-safety", "/solana/token-safety", "/compliance/wallet-screen", "/crypto/signal-fusion"],
        "receipt_signing": receipt_available(),
        "note": "100% internal composition; optional legs (wallet, signal) degrade gracefully, token-safety leg is required."})
