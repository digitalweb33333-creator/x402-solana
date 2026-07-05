"""PREMIUM-3 #1 — Capital Clearance Packet ($1.50).

The signed evidence bundle an autonomous agent acquires BEFORE moving significant
capital to a counterparty wallet. One call composes THREE deterministic on-chain
checks an agent would otherwise buy separately, and fuses them into a single
amount-scoped verdict with a short expiry:

  (1) sanctions/AML   — /compliance/wallet-screen (OFAC direct, sanctioned mixer, 1-hop exposure, wallet age)
  (2) fund forensics  — /crypto/wallet-forensics (multi-hop ERC-20 flow graph + patterns: circular, concentration, hubs)
  (3) counterparty    — /x402/seller-trust (on-chain settlement reputation: diversity, wash/sybil, age)

Output: CLEAR / REVIEW / BLOCK + the evaluated amount AND a policy-bounded approved
amount + structured reasons + a short EXPIRY (the packet perishes -> re-buy at the
next capital move) + a sha256 evidence hash over the composed facts + an Ed25519
signature verifiable offline. The stake (amount) is literally in the output.

Boundary (anti-cannibalisation):
- /agent/clearance ($0.25, kind=agent_action_clearance) = generic destructive-action
  guardrail, deterministic regex rules, NO on-chain data ("should I run this action?").
- THIS ($1.50, kind=capital_clearance_packet) = crypto capital move: real AML screening
  + multi-hop forensics + counterparty trust ("can I send this capital to THIS wallet?").
Distinct route AND distinct receipt `kind` so offline verifiers never confuse the two.

100% deterministic composition (no LLM in the verdict — the value is a MEASURED,
signed decision). Internal calls only; no new source, no new key. The compliance leg
is the core: if the counterparty is directly sanctioned the packet is BLOCK; the two
enrichment legs (forensics, trust) degrade gracefully and never fail the call.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.routers import seller_trust, wallet_forensics, wallet_screen
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

POLICY_VERSION = "capital-clearance-policy-2026-07-1"
PACKET_TTL_SECONDS = 600  # capital-move packet expires after 10 min → re-buy at next move
SOURCES = [
    "Internal /compliance/wallet-screen (OFAC SDN crypto + sanctioned mixer + 1-hop exposure + age)",
    "Internal /crypto/wallet-forensics (multi-hop ERC-20 flow graph + pattern detection)",
    "Internal /x402/seller-trust (Base settlement graph: diversity, wash/sybil, wallet age)",
]
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Amount bands (USD) — policy pinned by POLICY_VERSION.
AUTO_CEILING = 1.0        # trivial stake
CLEAR_CEILING = 100.0     # below this, a clean counterparty auto-clears
REVIEW_CEILING = 5000.0   # above this, only a TRUSTED counterparty auto-clears
YOUNG_WALLET_DAYS = 7.0

# Approved-amount ceiling implied by the counterparty trust verdict.
_TRUST_CEILING = {"TRUSTED": None, "CAUTION": 500.0, "ABSTAIN": 100.0, "AVOID": 0.0}


def _validate(to: str, amount_usd: float | None, depth: int) -> None:
    if not _ADDR_RE.match((to or "").strip()):
        raise HTTPException(status_code=400, detail={"code": "BAD_COUNTERPARTY",
                            "message": "'to' must be the counterparty EVM wallet (0x + 40 hex)."})
    if amount_usd is None or not isinstance(amount_usd, (int, float)) or amount_usd <= 0:
        raise HTTPException(status_code=400, detail={"code": "BAD_AMOUNT",
                            "message": "'amount_usd' is required and must be a positive number (the capital about to move)."})
    if depth not in (1, 2, 3):
        raise HTTPException(status_code=400, detail={"code": "BAD_DEPTH", "message": "'depth' must be 1, 2 or 3."})


async def _screen_leg(to: str) -> dict[str, Any]:
    """Compliance core. Robust (returns ABSTAIN rather than raising if lists are down)."""
    data = await wallet_screen.screen(to, ["ethereum", "base"])
    exp = data.get("mixer_exposure") or {}
    age = data.get("wallet_age") or {}
    return {"available": True, "verdict": data.get("verdict"),
            "matched_lists": [m.get("list") for m in (data.get("matched_lists") or []) if m.get("list")],
            "mixer_exposed": bool(exp.get("exposed")),
            "mixer_hits": exp.get("hits") or [],
            "min_age_days": age.get("min_age_days"),
            "receipt": data.get("signed_compliance_receipt") or {}}


async def _forensics_leg(to: str, depth: int) -> dict[str, Any]:
    """Multi-hop fund forensics. Degrades gracefully (unknown wallet has no graph)."""
    try:
        data = await wallet_forensics.analyze(to, depth, "base")
        patterns = data.get("patterns") or []
        return {"available": True,
                "patterns": [{"pattern": p["pattern"], "severity": p["severity"], "detail": p["detail"]} for p in patterns],
                "pattern_codes": [p["pattern"] for p in patterns],
                "graph_summary": data.get("graph_summary") or {},
                "ofac_listed": bool((data.get("header") or {}).get("ofac_listed")),
                "receipt": data.get("signed_receipt") or {}}
    except HTTPException as exc:
        det = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {"available": False, "reason": det.get("code", f"http_{exc.status_code}"),
                "patterns": [], "pattern_codes": [], "ofac_listed": False}
    except Exception as exc:  # best-effort leg, never propagate
        return {"available": False, "reason": type(exc).__name__, "patterns": [], "pattern_codes": [], "ofac_listed": False}


async def _trust_leg(to: str) -> dict[str, Any]:
    """Counterparty on-chain settlement reputation. Degrades gracefully."""
    try:
        data = await seller_trust.assess(to, "shallow")
        return {"available": True, "verdict": data.get("verdict"), "trust_score": data.get("trust_score"),
                "metrics": data.get("metrics") or {}, "receipt": data.get("signed_receipt") or {}}
    except HTTPException as exc:
        det = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {"available": False, "reason": det.get("code", f"http_{exc.status_code}"), "verdict": None, "trust_score": None, "metrics": {}}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__, "verdict": None, "trust_score": None, "metrics": {}}


def _fuse(amount: float, screen: dict, forensics: dict, trust: dict) -> dict[str, Any]:
    """Deterministic fusion → CLEAR / REVIEW / BLOCK + policy-bounded approved amount."""
    reasons: list[dict] = []

    sv = screen.get("verdict")
    tv = trust.get("verdict") if trust.get("available") else None
    codes = set(forensics.get("pattern_codes") or [])
    circular = "CIRCULAR_FLOW" in codes
    concentration = "FLOW_CONCENTRATION" in codes
    age_days = screen.get("min_age_days")
    young = isinstance(age_days, (int, float)) and age_days < YOUNG_WALLET_DAYS

    # --- Hard block: direct sanctions / sanctioned mixer identity ---
    hard_block = sv == "BLOCK" or forensics.get("ofac_listed")
    if sv == "BLOCK":
        reasons.append(reason("SANCTIONS_MATCH", f"Counterparty screen is BLOCK ({', '.join(screen.get('matched_lists') or []) or 'sanctions/mixer match'}).", 1.0))
    if forensics.get("ofac_listed") and sv != "BLOCK":
        reasons.append(reason("FORENSICS_OFAC", "Forensics header flags the counterparty as OFAC-listed.", 1.0))

    # --- Contributing signals ---
    if screen.get("mixer_exposed"):
        reasons.append(reason("MIXER_EXPOSURE", f"1-hop exposure to {len(screen.get('mixer_hits') or [])} sanctioned/mixer address(es).", 0.6))
    if circular:
        reasons.append(reason("CIRCULAR_FLOW", "Funds cycle back to origin through intermediary wallet(s) (layering signature).", 0.6))
    if concentration:
        reasons.append(reason("FLOW_CONCENTRATION", "Transfer flow is concentrated in a single counterparty.", 0.3))
    if tv == "AVOID":
        reasons.append(reason("COUNTERPARTY_AVOID", f"Counterparty on-chain trust is AVOID (score {trust.get('trust_score')}).", 0.6))
    elif tv == "CAUTION":
        reasons.append(reason("COUNTERPARTY_CAUTION", f"Counterparty on-chain trust is CAUTION (score {trust.get('trust_score')}).", 0.3))
    elif tv == "TRUSTED":
        reasons.append(reason("COUNTERPARTY_TRUSTED", f"Counterparty on-chain trust is TRUSTED (score {trust.get('trust_score')}).", -0.4))
    elif tv == "ABSTAIN" or not trust.get("available"):
        reasons.append(reason("COUNTERPARTY_UNKNOWN", "Insufficient on-chain settlement history to establish counterparty trust.", 0.25))
    if sv == "ABSTAIN":
        reasons.append(reason("SCREEN_ABSTAIN", "Sanctions lists could not be loaded — cannot positively clear.", 0.4))
    elif sv == "PASS":
        reasons.append(reason("SCREEN_PASS", "No sanctions/mixer match on the counterparty.", -0.4))
    if young:
        reasons.append(reason("YOUNG_COUNTERPARTY", f"Counterparty first seen only ~{age_days} days ago.", 0.3))

    # --- Approved-amount ceiling from counterparty trust ---
    ceiling = _TRUST_CEILING.get(tv, 100.0)          # None = uncapped (TRUSTED)
    if not trust.get("available"):
        ceiling = 100.0                               # unknown counterparty → conservative cap
    approved = amount if ceiling is None else min(amount, ceiling)

    serious = hard_block or sv in ("BLOCK", "WARN") or screen.get("mixer_exposed") or circular or tv == "AVOID" or sv == "ABSTAIN"

    # --- Verdict (deterministic) ---
    if hard_block:
        verdict, approved = "BLOCK", 0.0
    elif serious:
        verdict = "REVIEW"
    elif approved < amount:
        verdict = "REVIEW"; reasons.append(reason("AMOUNT_EXCEEDS_CEILING", f"Requested ${amount:.2f} exceeds the amount this counterparty's history supports (${approved:.2f}).", 0.4))
    elif amount > REVIEW_CEILING and tv != "TRUSTED":
        verdict = "REVIEW"; reasons.append(reason("LARGE_MOVE", f"Stake ${amount:.2f} above ${REVIEW_CEILING:.0f} requires a TRUSTED counterparty to auto-clear.", 0.5))
    elif young and amount > CLEAR_CEILING:
        verdict = "REVIEW"; reasons.append(reason("YOUNG_AND_MATERIAL", f"Material stake ${amount:.2f} to a wallet under {YOUNG_WALLET_DAYS:.0f} days old.", 0.4))
    else:
        verdict = "CLEAR"; approved = amount
        reasons.append(reason("WITHIN_POLICY", "Counterparty clean and stake within the auto-clearance policy.", -0.3))

    # confidence: rises with how many legs resolved and how decisive the compliance leg is.
    legs_ok = 1 + int(forensics.get("available", False)) + int(trust.get("available", False))
    base = {"BLOCK": 0.95, "REVIEW": 0.7, "CLEAR": 0.8}[verdict]
    confidence = round(clamp01(base * (0.75 + 0.08 * legs_ok)), 3)

    risk = clamp01(sum(r["weight"] for r in reasons if r["weight"] > 0))
    return {"verdict": verdict, "confidence": confidence, "risk_score": int(round(risk * 100)),
            "approved_amount_usd": round(approved, 2), "reasons": reasons}


def _evidence_hash(to: str, amount: float, screen: dict, forensics: dict, trust: dict, fused: dict) -> str:
    payload = {
        "policy_version": POLICY_VERSION, "to": to.lower(), "amount_usd": amount,
        "screen_verdict": screen.get("verdict"), "screen_matched": sorted(screen.get("matched_lists") or []),
        "mixer_exposed": screen.get("mixer_exposed"),
        "forensics_patterns": sorted(forensics.get("pattern_codes") or []),
        "trust_verdict": trust.get("verdict"), "trust_score": trust.get("trust_score"),
        "decision": fused["verdict"], "approved_amount_usd": fused["approved_amount_usd"],
        # pin to the component receipts' signatures (tamper-evident chain)
        "component_signatures": [
            (screen.get("receipt") or {}).get("signature"),
            (forensics.get("receipt") or {}).get("signature"),
            (trust.get("receipt") or {}).get("signature"),
        ],
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def evaluate(to: str, amount_usd: float, depth: int) -> dict[str, Any]:
    _validate(to, amount_usd, depth)
    addr = to.strip()
    amount = float(amount_usd)

    # Core compliance leg first (may 400 on bad address — already validated). Then the two
    # enrichment legs concurrently. Compliance is robust; forensics/trust degrade gracefully.
    screen = await _screen_leg(addr)
    forensics, trust = await asyncio.gather(_forensics_leg(addr, depth), _trust_leg(addr))

    fused = _fuse(amount, screen, forensics, trust)
    ev_hash = _evidence_hash(addr, amount, screen, forensics, trust, fused)
    evaluated_at = now_iso()
    expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + PACKET_TTL_SECONDS))

    packet = sign_receipt({
        "kind": "capital_clearance_packet",
        "decision": fused["verdict"],
        "policy_version": POLICY_VERSION,
        "counterparty": addr,
        "evaluated_amount_usd": round(amount, 2),
        "approved_amount_usd": fused["approved_amount_usd"],
        "currency": "USD",
        "evidence_hash": ev_hash,
        "screen_verdict": screen.get("verdict"),
        "forensics_patterns": forensics.get("pattern_codes") or [],
        "trust_verdict": trust.get("verdict"),
        "trust_score": trust.get("trust_score"),
        "expiry": expiry,
        "as_of": evaluated_at,
    })

    return {
        "verdict": fused["verdict"],
        "confidence": fused["confidence"],
        "risk_score": fused["risk_score"],
        "evaluated_amount_usd": round(amount, 2),
        "approved_amount_usd": fused["approved_amount_usd"],
        "currency": "USD",
        "reasons": fused["reasons"],
        "query": {"to": addr, "amount_usd": amount, "depth": depth},
        "components": {
            "sanctions_screen": {"available": screen.get("available"), "verdict": screen.get("verdict"),
                                 "matched_lists": screen.get("matched_lists"), "mixer_exposed": screen.get("mixer_exposed"),
                                 "mixer_hits": screen.get("mixer_hits"), "counterparty_age_days": screen.get("min_age_days")},
            "fund_forensics": {"available": forensics.get("available"), "reason": forensics.get("reason"),
                               "patterns": forensics.get("patterns"), "graph_summary": forensics.get("graph_summary")},
            "counterparty_trust": {"available": trust.get("available"), "reason": trust.get("reason"),
                                   "verdict": trust.get("verdict"), "trust_score": trust.get("trust_score"),
                                   "metrics": trust.get("metrics")},
        },
        "timing": {"evaluated_at": evaluated_at, "expiry": expiry, "ttl_seconds": PACKET_TTL_SECONDS,
                   "note": "Packet expires; re-acquire before the next capital move — conditions change on-chain."},
        "evidence_hash": ev_hash,
        "clearance_packet": packet,
        "data_freshness": freshness(evaluated_at, deterministic=True, sources=SOURCES,
                                    extra={"policy_version": POLICY_VERSION, "legs_available": [
                                        k for k, v in {"sanctions_screen": screen.get("available"),
                                                       "fund_forensics": forensics.get("available"),
                                                       "counterparty_trust": trust.get("available")}.items() if v]}),
        "error": None,
        "source": " + ".join(SOURCES),
        "timestamp": evaluated_at,
        "disclaimer": "Signed pre-capital-move clearance composed from on-chain sanctions screening, multi-hop "
                      "forensics and counterparty settlement reputation. Deterministic and amount-scoped; a CLEAR "
                      "verdict attests the checks passed at issuance, not the ultimate safety of the transfer. "
                      "Not legal/financial advice.",
        "cached": False,
    }


@router.get("/agent/clearance-packet")
async def clearance_packet_get(
    to: str = Query(..., description="Counterparty EVM wallet the capital will move to (0x + 40 hex)."),
    amount_usd: float = Query(..., description="Capital about to move, in USD, e.g. 800. Scopes the verdict and the approved amount."),
    depth: int = Query(2, description="Forensics graph hops to traverse: 1, 2 or 3 (default 2)."),
) -> JSONResponse:
    """GET /agent/clearance-packet — signed CLEAR/REVIEW/BLOCK packet before a capital move: fuses sanctions screen + multi-hop forensics + counterparty trust, amount-scoped, with a short expiry."""
    return JSONResponse(content=await evaluate(to, amount_usd, depth))


@router.get("/agent/clearance-packet/health")
async def clearance_packet_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "clearance-packet", "status": "ok",
        "composition": ["/compliance/wallet-screen", "/crypto/wallet-forensics", "/x402/seller-trust"],
        "policy_version": POLICY_VERSION, "ttl_seconds": PACKET_TTL_SECONDS,
        "receipt_signing": receipt_available(),
        "note": "100% deterministic composition (no LLM in the verdict). Compliance leg is core; forensics/trust degrade gracefully."})
