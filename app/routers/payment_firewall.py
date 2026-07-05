"""Endpoint 2 — x402 Payment Firewall / Pre-Payment Risk Check.

Avant qu'un agent n'ENVOIE un paiement, il poste {to_address, amount, chain,
expected_price?} et reçoit un verdict ALLOW / REVIEW / BLOCK en <200 ms.

Angle (cf benchmark) : les firewalls existants (PaySentry, Frisk) sont des SDK
gratuits OFFLINE — ils ne voient que la session locale. Notre edge = une donnée
qu'un SDK offline ne peut pas avoir : une blocklist OFAC fraîche partagée +
une baseline d'anomalie inter-appels (les montants vus par CE service). On NE
revend pas un simple plafond de budget (ça, PaySentry le donne gratuitement).

5 règles machine-readable respectées : verdict en haut, confidence + reasons[]
{code,label,weight}, data_freshness/deterministic/sources, codes d'erreur énumérés,
droit d'ABSTAIN si la blocklist n'a pas pu charger.

Sources : OFAC SDN crypto (0xB10C mirror, gratuit) + validation de chaîne locale +
baseline statistique interne. TTL liste 24 h. Tier $0.005.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.sources.sanctions_screen import TORNADO_CASH, chain_of, sanctioned_sets
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = ["OFAC SDN crypto addresses (0xB10C mirror)", "local chain validation", "internal spend baseline"]
_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Chaînes connues (id x402/CAIP-ish ou nom). Sert à valider, pas à bloquer une chaîne exotique.
KNOWN_CHAINS = {
    "base": "evm", "eip155:8453": "evm", "ethereum": "evm", "eip155:1": "evm",
    "arbitrum": "evm", "optimism": "evm", "polygon": "evm", "bsc": "evm",
    "avalanche": "evm", "solana": "solana", "bitcoin": "bitcoin", "tron": "tron",
}
# Plafond absolu d'un appel x402 « micro » : au-delà, un humain devrait revoir.
ABS_REVIEW_USD = 50.0
ABS_BLOCK_USD = 5000.0
# Multiple du prix attendu au-delà duquel on suspecte une dépense incontrôlée.
RUNAWAY_MULT = 4.0

# Baseline d'anomalie inter-appels : derniers montants observés (mémoire process).
_recent_amounts: deque[float] = deque(maxlen=500)


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


async def assess(to_address: str, amount: float | None, chain: str | None,
                 expected_price: float | None) -> dict[str, Any]:
    addr = (to_address or "").strip()
    ch = (chain or "").strip().lower()
    reasons: list[dict] = []

    evm_set, other_set, loaded = await sanctioned_sets()
    list_ok = bool(evm_set or other_set)

    # --- Validation de l'adresse (format vs chaîne déclarée/détectée) ---
    detected = chain_of(addr)
    addr_valid = detected != "unknown"
    declared_kind = KNOWN_CHAINS.get(ch)
    chain_known = ch in KNOWN_CHAINS
    if not chain_known and ch:
        reasons.append(reason("UNKNOWN_CHAIN", f"Chain '{ch}' not in known set; validated by address format only", 0.15))
    if declared_kind and detected != "unknown" and declared_kind != detected:
        reasons.append(reason("CHAIN_ADDRESS_MISMATCH", f"Address looks like {detected} but chain says {ch}", 0.4))

    # --- Blocklist (déterministe) ---
    sanctioned = False
    is_mixer = False
    if detected == "evm":
        sanctioned = addr.lower() in evm_set
        is_mixer = addr.lower() in TORNADO_CASH
    elif detected != "unknown":
        sanctioned = addr in other_set
    if sanctioned:
        reasons.append(reason("OFAC_SANCTIONED_RECIPIENT", "Recipient is on the OFAC SDN crypto list", 1.0))
    if is_mixer:
        reasons.append(reason("KNOWN_MIXER_RECIPIENT", "Recipient is a sanctioned mixer (Tornado Cash)", 0.9))

    # --- Anomalie de montant / dépense incontrôlée ---
    amt = amount if (amount is not None and amount >= 0) else None
    if amt is not None:
        if expected_price is not None and expected_price > 0 and amt > expected_price * RUNAWAY_MULT:
            reasons.append(reason("AMOUNT_EXCEEDS_EXPECTED",
                                  f"Amount ${amt:g} is >{RUNAWAY_MULT:g}x the expected ${expected_price:g}", 0.7))
        if amt >= ABS_BLOCK_USD:
            reasons.append(reason("AMOUNT_FAR_ABOVE_MICRO", f"Amount ${amt:g} is far above any x402 micro-call", 0.9))
        elif amt >= ABS_REVIEW_USD:
            reasons.append(reason("AMOUNT_ABOVE_MICRO", f"Amount ${amt:g} is high for an x402 micro-call", 0.45))
        # baseline inter-appels
        snapshot = sorted(_recent_amounts)
        p95 = _percentile(snapshot, 0.95)
        if p95 is not None and len(snapshot) >= 30 and amt > max(p95 * 5, 1.0):
            reasons.append(reason("ABOVE_NETWORK_BASELINE",
                                  f"Amount ${amt:g} is >5x this service's p95 (${p95:g})", 0.35))
        _recent_amounts.append(amt)

    # --- Verdict ---
    if not addr_valid:
        verdict = "BLOCK"
        confidence = 0.95
        reasons.insert(0, reason("INVALID_RECIPIENT", "Recipient address is not a valid address on any known chain", 1.0))
    elif sanctioned or is_mixer or (amt is not None and amt >= ABS_BLOCK_USD):
        verdict = "BLOCK"
        confidence = 0.97 if list_ok else 0.7
    elif not list_ok:
        # Droit de s'abstenir : sans blocklist fraîche, on ne peut pas garantir un ALLOW.
        verdict = "ABSTAIN"
        confidence = 0.3
        reasons.append(reason("BLOCKLIST_UNAVAILABLE", "OFAC blocklist could not be loaded; cannot clear recipient", 0.5))
    else:
        risk = sum(r["weight"] for r in reasons if r["weight"] > 0)
        if risk >= 0.6:
            verdict, confidence = "REVIEW", clamp01(0.6 + risk / 4)
        elif risk > 0:
            verdict, confidence = "REVIEW", clamp01(0.55 + risk)
        else:
            verdict, confidence = "ALLOW", 0.9

    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "reasons": reasons or [reason("NO_RISK_SIGNAL", "No blocklist hit or spend anomaly detected", -0.5)],
        "query": {"to_address": addr, "amount": amount, "chain": chain, "expected_price": expected_price},
        "recipient": {"detected_chain": detected, "valid": addr_valid,
                      "ofac_sanctioned": sanctioned, "known_mixer": is_mixer},
        "data_freshness": freshness(now_iso(), deterministic=True, sources=SOURCES,
                                    extra={"ofac_lists_loaded": loaded, "blocklist_size_evm": len(evm_set)}),
        "error": None,
        "timestamp": now_iso(),
        "disclaimer": "Automated pre-payment risk screen, not legal/financial advice. ALLOW is not a guarantee.",
    }


@router.get("/x402/payment-firewall")
async def payment_firewall(
    to_address: str = Query(..., description="Recipient/payTo address, e.g. '0x...' (EVM), Solana/BTC/TRON also accepted"),
    amount: float | None = Query(None, description="Payment amount in USD, e.g. 0.01"),
    chain: str | None = Query(None, description="base | ethereum | solana | bitcoin | tron | … (validated by address format if omitted)"),
    expected_price: float | None = Query(None, description="Price the resource advertised, e.g. 0.01 — flags runaway overspend"),
) -> JSONResponse:
    """GET /x402/payment-firewall — pre-payment verdict ALLOW/REVIEW/BLOCK on {recipient, amount, chain}."""
    if not (to_address or "").strip():
        return JSONResponse(status_code=400, content={"error": {"code": "MISSING_RECIPIENT", "message": "'to_address' is required."}})
    return JSONResponse(content=await assess(to_address, amount, chain, expected_price))


@router.get("/x402/payment-firewall/health")
async def payment_firewall_health() -> JSONResponse:
    evm, other, loaded = await sanctioned_sets()
    ok = bool(evm or other)
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "payment-firewall", "status": "ok" if ok else "degraded",
        "upstream": {"ofac_lists_loaded": loaded, "blocklist_size_evm": len(evm)},
        "baseline_samples": len(_recent_amounts)})
