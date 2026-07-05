"""Endpoint 1 — x402 Seller Trust Score (méta-confiance).

Avant qu'un agent ne paie un endpoint x402 inconnu, il score le WALLET VENDEUR à
partir de son graphe de règlement on-chain (Base). « Le D&B des agents ».

Angle (cf benchmark) : TWZRD fait ça sur Solana (sans traction publiée) ; AgentRadar
a de la traction mais score l'IDENTITÉ ERC-8004 de l'ACHETEUR, pas la qualité de
règlement du VENDEUR. La voie ouverte = Base/EVM, analyse du graphe de règlement
USDC réel (contreparties, ancienneté, concentration wash-trade, sybil), vendue comme
VERDICT payant + reçu signé vérifiable hors-ligne (≠ score gratuit de TWZRD).

5 règles : verdict TRUSTED/CAUTION/AVOID en haut, confidence + reasons[] {code,label,
weight}, data_freshness/deterministic/sources, codes d'erreur, ABSTAIN si wallet trop
récent / trop peu de règlements pour juger.

Source : Base Blockscout (transferts USDC entrants = proxy de règlement x402) + OFAC.
Tier $0.01. TTL 5 min.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.sources.base_chain import usdc_transfers_in
from app.sources.http_util import TTLCache
from app.sources.sanctions_screen import ofac_evm_set
from app.verdict import age_seconds, clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = ["Base Blockscout USDC transfers (settlement proxy)", "OFAC SDN crypto addresses"]
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(300)

MIN_SETTLEMENTS = 3          # en dessous : ABSTAIN (trop peu pour juger)
MIN_AGE_DAYS_FOR_TRUST = 7   # plus jeune : plafonné à CAUTION


def _oldest_ts(transfers: list[dict]) -> str | None:
    times = [t["ts"] for t in transfers if t.get("ts")]
    return min(times) if times else None


def _score(transfers: list[dict], sanctioned: bool) -> dict[str, Any]:
    reasons: list[dict] = []
    n = len(transfers)
    senders = [t["from"] for t in transfers if t.get("from")]
    unique = len(set(senders))
    volume = sum(t["value_usdc"] for t in transfers if t.get("value_usdc")) or 0.0
    oldest = _oldest_ts(transfers)
    age_days = (age_seconds(oldest) or 0) / 86400.0

    # Concentration / wash-trade : part du contributeur le plus fréquent.
    counts = Counter(senders)
    top_share = (counts.most_common(1)[0][1] / n) if n else 0.0
    distinct_ratio = (unique / n) if n else 0.0
    # Sybil : beaucoup de senders « one-shot » avec des montants quasi identiques.
    one_shot = sum(1 for _, c in counts.items() if c == 1)
    one_shot_ratio = (one_shot / unique) if unique else 0.0
    vals = [round(t["value_usdc"], 4) for t in transfers if t.get("value_usdc")]
    amount_uniformity = (Counter(vals).most_common(1)[0][1] / len(vals)) if vals else 0.0

    score = 50.0
    # Demande réelle diversifiée = rassurant.
    score += min(30.0, unique * 2.0); reasons.append(reason("UNIQUE_COUNTERPARTIES", f"{unique} distinct paying counterparties", -min(0.5, unique * 0.03)))
    score += min(15.0, n / 2.0)
    score += min(15.0, age_days / 4.0)
    if age_days < MIN_AGE_DAYS_FOR_TRUST:
        reasons.append(reason("YOUNG_WALLET", f"First settlement only {age_days:.1f} days ago", 0.3))

    # Wash-trade : un seul payeur domine.
    if n >= 5 and top_share >= 0.8:
        score -= 25.0; reasons.append(reason("SINGLE_COUNTERPARTY_DOMINANCE",
                                              f"{top_share*100:.0f}% of settlements come from ONE counterparty (possible wash/self-dealing)", 0.7))
    elif n >= 8 and distinct_ratio < 0.25:
        score -= 12.0; reasons.append(reason("LOW_COUNTERPARTY_DIVERSITY",
                                              f"Only {unique} payers for {n} settlements", 0.4))
    # Sybil : nuée de payeurs uniques à montant identique.
    if unique >= 10 and one_shot_ratio > 0.9 and amount_uniformity > 0.8:
        score -= 18.0; reasons.append(reason("SYBIL_PATTERN",
                                              "Many one-shot payers with near-identical amounts (sybil-like)", 0.6))
    if sanctioned:
        score = 0.0; reasons.append(reason("OFAC_SANCTIONED", "Seller wallet is on the OFAC SDN crypto list", 1.0))

    score = max(0.0, min(100.0, score))
    return {
        "trust_score": round(score),
        "reasons": reasons,
        "metrics": {
            "settlement_count": n, "unique_counterparties": unique,
            "received_volume_usdc": round(volume, 2), "first_settlement": oldest,
            "wallet_age_days": round(age_days, 1),
            "top_counterparty_share": round(top_share, 3),
            "counterparty_diversity_ratio": round(distinct_ratio, 3),
            "one_shot_payer_ratio": round(one_shot_ratio, 3),
            "amount_uniformity": round(amount_uniformity, 3),
        },
    }


async def assess(seller_wallet: str, depth: str) -> dict[str, Any]:
    addr = (seller_wallet or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ADDRESS",
                            "message": "'seller_wallet' must be an EVM address (0x + 40 hex)."})
    deep = (depth or "").strip().lower() in ("deep", "full", "true", "1")
    key = f"{addr.lower()}|{'deep' if deep else 'shallow'}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    transfers, err = await usdc_transfers_in(addr, max_pages=10 if deep else 4)
    ofac = await ofac_evm_set()
    if transfers is None:
        raise HTTPException(status_code=502, detail={"code": "CHAIN_SOURCE_UNAVAILABLE",
                            "message": f"Base settlement source unreachable ({err}); not charged."})
    sanctioned = addr.lower() in ofac

    n = len(transfers)
    sc = _score(transfers, sanctioned)
    oldest = sc["metrics"]["first_settlement"]

    # --- Verdict + droit d'ABSTAIN ---
    if sanctioned:
        verdict, confidence, error = "AVOID", 0.97, None
    elif n == 0:
        verdict, confidence = "ABSTAIN", 0.4
        error = {"code": "NO_SETTLEMENT_HISTORY",
                 "message": "No USDC settlements found on Base for this wallet — cannot establish trust."}
    elif n < MIN_SETTLEMENTS:
        verdict, confidence = "ABSTAIN", 0.45
        error = {"code": "INSUFFICIENT_HISTORY",
                 "message": f"Only {n} settlement(s) (<{MIN_SETTLEMENTS}) — too little history to judge."}
    else:
        error = None
        risk = sum(r["weight"] for r in sc["reasons"] if r["weight"] > 0)
        s = sc["trust_score"]
        age_days = sc["metrics"]["wallet_age_days"]
        dominance = sc["metrics"]["top_counterparty_share"] >= 0.8 and n >= 5
        if s < 40 or dominance or risk >= 1.1:
            verdict = "AVOID"          # bad score, single-payer wash, or stacked red flags
        elif s >= 70 and risk < 0.5 and age_days >= MIN_AGE_DAYS_FOR_TRUST:
            verdict = "TRUSTED"
        else:
            verdict = "CAUTION"        # young / thin diversity but no hard red flag
        # confidence croît avec le volume de données
        confidence = clamp01(0.5 + min(0.4, n / 100.0) + min(0.1, sc["metrics"]["unique_counterparties"] / 50.0))

    receipt = sign_receipt({
        "kind": "x402_seller_trust",
        "seller_wallet": addr,
        "verdict": verdict,
        "trust_score": sc["trust_score"],
        "settlement_count": n,
        "unique_counterparties": sc["metrics"]["unique_counterparties"],
        "as_of": oldest or now_iso(),
    })

    shaped = {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "trust_score": sc["trust_score"],
        "reasons": sc["reasons"],
        "query": {"seller_wallet": addr, "depth": "deep" if deep else "shallow"},
        "metrics": sc["metrics"],
        "signed_receipt": receipt,
        "data_freshness": freshness(oldest, deterministic=True, sources=SOURCES,
                                    extra={"chain": "base", "ofac_list_loaded": bool(ofac)}),
        "error": error,
        "timestamp": now_iso(),
        "disclaimer": "Trust derived from incoming USDC transfers on Base as a settlement proxy; "
                      "not all incoming USDC is an x402 settlement. Not financial/compliance advice.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/x402/seller-trust")
async def seller_trust(
    seller_wallet: str = Query(..., description="x402 seller payTo wallet to score, e.g. '0x1D1B...620f' (EVM)"),
    depth: str = Query("shallow", description="'shallow' (last ~200 settlements) or 'deep' (~500)"),
) -> JSONResponse:
    """GET /x402/seller-trust — score a seller wallet TRUSTED/CAUTION/AVOID from its on-chain settlement graph + signed receipt."""
    return JSONResponse(content=await assess(seller_wallet, depth))


@router.get("/x402/seller-trust/health")
async def seller_trust_health() -> JSONResponse:
    transfers, err = await usdc_transfers_in("0x1D1B81247C407521E2A01F3E21514870dcf1620f", max_pages=1)
    ok = transfers is not None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "seller-trust", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCES[0], "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
