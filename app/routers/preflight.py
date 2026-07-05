"""Endpoint #3 (LOT 9) — Agent Action Preflight + signed ClearancePacket ($0.25).

Avant qu'un agent n'exécute une action DESTRUCTRICE / COÛTEUSE / IRRÉVERSIBLE
(delete, deploy, transférer des fonds, publier, écraser…), il appelle ce préflight
et reçoit : évaluation de risque + check de réversibilité + timing + verdict
{CLEAR, LIMIT, REVIEW, BLOCK} + un ClearancePacket SIGNÉ (Ed25519) que l'agent garde
comme preuve d'autorisation (décision, montant approuvé, version de politique, hash des
preuves, expiry, rationale). Infra de sécurité agent : éviter une erreur bien plus
coûteuse que le prix de l'appel.

Benchmark (cf BILAN) : AgentSpendGuard expose un /preflight proche (risque + réversibilité
+ timing + allow/caution/block) à $0.025 — MOINS CHER. CONTESTÉ. L'edge retenu et assumé :
(1) ClearancePacket cryptographiquement SIGNÉ, vérifiable hors-ligne, épinglé à une version
de politique (artefact d'audit/compliance) ; (2) périmètre ÉLARGI à TOUTE action d'agent
(pas seulement payer un endpoint x402) ; (3) verdict à 4 états + montant approuvé borné.

100% déterministe (rules engine). Pas de LLM. /health vert, jamais de 500 nu.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

from app.receipt import sign_receipt
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

POLICY_VERSION = "clearance-policy-2026-06-1"
PACKET_TTL_SECONDS = 300  # le ClearancePacket expire après 5 min
SOURCES = ["Local deterministic clearance rules engine (x402-endpoints LOT9)"]

# Seuils de montant (USD) — politique figée, épinglée par POLICY_VERSION.
AUTO_CEILING = 1.0       # <= : trivial
CLEAR_CEILING = 50.0     # <= : CLEAR si réversible
LIMIT_CEILING = 500.0    # <= : LIMIT (montant borné)
REVIEW_CEILING = 5000.0  # <= : REVIEW ; au-delà : BLOCK

ACTION_TYPES = {"delete", "deploy", "send", "spend", "transfer", "publish", "email",
                "overwrite", "approve", "rotate", "shutdown", "other"}

_IRREVERSIBLE_RX = re.compile(
    r"\b(?:delete|destroy|drop|wipe|erase|burn|terminate|purge|truncate|format|"
    r"irreversible|permanent(?:ly)?|cannot\s+be\s+undone|unrecoverable|"
    r"on[-\s]?chain|transfer|send\s+funds|broadcast|mint|revoke)\b", re.I)
_DESTRUCTIVE_RX = re.compile(
    r"\b(?:delete|destroy|drop\s+table|rm\s+-rf|wipe|overwrite|truncate|"
    r"shutdown|terminate|revoke|disable|uninstall|force[-\s]?push)\b", re.I)
_EXTERNAL_RX = re.compile(
    r"\b(?:publish|post|tweet|email|send\s+message|deploy|go[-\s]?live|"
    r"production|prod\b|broadcast|notify\s+all|press\s+release)\b", re.I)
# action_type intrinsèquement irréversible (transfert de valeur / suppression).
_IRREVERSIBLE_TYPES = {"delete", "transfer", "send", "spend", "publish", "email"}


class PreflightRequest(BaseModel):
    action: str = Field(..., description="Plain-language description of the action about to run, e.g. 'delete the production users table' or 'transfer 250 USDC to 0xabc...'.")
    action_type: str | None = Field(None, description="Optional: delete | deploy | send | spend | transfer | publish | email | overwrite | approve | rotate | shutdown | other.")
    amount_usd: float | None = Field(None, description="Monetary stake in USD if applicable, e.g. 250. Omit for non-financial actions.")
    reversible: bool | None = Field(None, description="Optional caller hint: is the action reversible? If omitted it is inferred from the description/type.")
    target: str | None = Field(None, description="Optional target of the action, e.g. a table name, address, repo, recipient.")
    context: str | None = Field(None, description="Optional extra context the policy can use (environment, prior approval, etc.).")


def _evidence_hash(req: PreflightRequest) -> str:
    payload = {
        "action": req.action, "action_type": req.action_type, "amount_usd": req.amount_usd,
        "reversible": req.reversible, "target": req.target, "context": req.context,
        "policy_version": POLICY_VERSION,
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _assess(req: PreflightRequest) -> dict[str, Any]:
    reasons: list[dict] = []
    action = req.action or ""
    atype = (req.action_type or "").strip().lower()
    amount = req.amount_usd if (isinstance(req.amount_usd, (int, float)) and req.amount_usd >= 0) else None

    # --- Réversibilité ---
    if req.reversible is not None:
        reversible = bool(req.reversible)
        rev_source = "caller_hint"
    else:
        irreversible = bool(_IRREVERSIBLE_RX.search(action)) or atype in _IRREVERSIBLE_TYPES
        reversible = not irreversible
        rev_source = "inferred"
    if not reversible:
        reasons.append(reason("IRREVERSIBLE", "Action appears irreversible / hard to undo", 0.5))
    else:
        reasons.append(reason("REVERSIBLE", "Action appears reversible", -0.2))

    destructive = bool(_DESTRUCTIVE_RX.search(action)) or atype in ("delete", "shutdown", "overwrite")
    if destructive:
        reasons.append(reason("DESTRUCTIVE", "Action contains destructive verbs", 0.4))
    external = bool(_EXTERNAL_RX.search(action)) or atype in ("publish", "email", "deploy")
    if external:
        reasons.append(reason("EXTERNAL_EFFECT", "Action has outward/irrecoverable external effect", 0.3))

    # --- Risque financier ---
    fin_band = "none"
    if amount is not None and amount > 0:
        if amount <= AUTO_CEILING:
            fin_band = "trivial"
        elif amount <= CLEAR_CEILING:
            fin_band = "low"; reasons.append(reason("SPEND_LOW", f"Stake ${amount:.2f} within low band", 0.1))
        elif amount <= LIMIT_CEILING:
            fin_band = "moderate"; reasons.append(reason("SPEND_MODERATE", f"Stake ${amount:.2f} above the auto limit", 0.35))
        elif amount <= REVIEW_CEILING:
            fin_band = "high"; reasons.append(reason("SPEND_HIGH", f"Stake ${amount:.2f} requires review", 0.6))
        else:
            fin_band = "extreme"; reasons.append(reason("SPEND_EXTREME", f"Stake ${amount:.2f} exceeds the hard ceiling", 0.9))

    # --- Score de risque agrégé (0-1) ---
    risk = 0.0
    risk += 0.0 if reversible else 0.45
    risk += 0.25 if destructive else 0.0
    risk += 0.2 if external else 0.0
    risk += {"none": 0.0, "trivial": 0.0, "low": 0.1, "moderate": 0.3, "high": 0.55, "extreme": 0.85}[fin_band]
    risk = clamp01(risk)

    # --- Verdict 4 états (déterministe) + montant approuvé ---
    approved_amount = amount
    if fin_band == "extreme":
        verdict = "BLOCK"; approved_amount = 0.0
        reasons.append(reason("HARD_CEILING", "Stake over the absolute policy ceiling — blocked", 1.0))
    elif (not reversible and (destructive or external)) or fin_band == "high":
        verdict = "REVIEW"; approved_amount = 0.0 if amount else None
    elif fin_band == "moderate":
        verdict = "LIMIT"; approved_amount = LIMIT_CEILING  # autorisé mais borné
    elif (not reversible) and amount is None and destructive:
        # irréversible + destructeur, pas de montant : passe en REVIEW (sécurité)
        verdict = "REVIEW"
    else:
        verdict = "CLEAR"
        if amount is not None:
            approved_amount = min(amount, CLEAR_CEILING)

    if verdict == "CLEAR":
        reasons.append(reason("WITHIN_POLICY", "Within auto-clearance policy", -0.3))

    # confidence : haute quand les signaux sont nets (montant fourni, hint réversibilité)
    confidence = clamp01(0.6 + (0.15 if req.reversible is not None else 0.0)
                         + (0.15 if amount is not None else 0.0)
                         + (0.1 if atype in ACTION_TYPES and atype else 0.0))

    return {
        "verdict": verdict, "confidence": round(confidence, 3),
        "risk_score": int(round(risk * 100)), "reasons": reasons,
        "reversibility": {"reversible": reversible, "source": rev_source, "destructive": destructive, "external_effect": external},
        "financial": {"amount_usd": amount, "band": fin_band, "approved_amount_usd": approved_amount},
        "timing": {
            "evaluated_at": now_iso(),
            "expiry": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + PACKET_TTL_SECONDS)),
            "ttl_seconds": PACKET_TTL_SECONDS,
            "note": "Re-run preflight if you act after expiry; conditions may have changed.",
        },
    }


def _rationale(a: dict[str, Any]) -> str:
    v = a["verdict"]
    rv = a["reversibility"]
    parts = []
    parts.append("reversible" if rv["reversible"] else "IRREVERSIBLE")
    if rv["destructive"]:
        parts.append("destructive")
    if rv["external_effect"]:
        parts.append("external effect")
    band = a["financial"]["band"]
    if band not in ("none", "trivial"):
        parts.append(f"{band} financial stake")
    base = ", ".join(parts)
    return {
        "CLEAR": f"Cleared: {base}. Within auto-clearance policy.",
        "LIMIT": f"Allowed but capped: {base}. Stake exceeds the auto limit; approved amount bounded.",
        "REVIEW": f"Human review required: {base}. Irreversible/high-impact action.",
        "BLOCK": f"Blocked: {base}. Exceeds the hard policy ceiling.",
    }[v]


@router.post("/agent/clearance")
async def preflight(req: PreflightRequest) -> JSONResponse:
    """POST /agent/clearance — preflight risk + reversibility + timing for a pending destructive/costly action; CLEAR/LIMIT/REVIEW/BLOCK + signed ClearancePacket."""
    a = _assess(req)
    ev_hash = _evidence_hash(req)
    rationale = _rationale(a)

    # ClearancePacket signé : faits matériels de la décision (vérifiable hors-ligne).
    packet = sign_receipt({
        "kind": "agent_action_clearance",
        "decision": a["verdict"],
        "policy_version": POLICY_VERSION,
        "evidence_hash": ev_hash,
        "approved_amount_usd": a["financial"]["approved_amount_usd"],
        "reversible": a["reversibility"]["reversible"],
        "expiry": a["timing"]["expiry"],
        "rationale": rationale,
    })

    shaped = {
        "verdict": a["verdict"],
        "confidence": a["confidence"],
        "risk_score": a["risk_score"],
        "reasons": a["reasons"],
        "query": {"action": req.action, "action_type": req.action_type, "amount_usd": req.amount_usd,
                  "target": req.target, "reversible_hint": req.reversible},
        "reversibility": a["reversibility"],
        "financial": a["financial"],
        "timing": a["timing"],
        "rationale": rationale,
        "clearance_packet": packet,
        "data_freshness": freshness(now_iso(), deterministic=True, sources=SOURCES,
                                    extra={"policy_version": POLICY_VERSION, "evidence_hash": ev_hash}),
        "error": None,
        "timestamp": now_iso(),
        "disclaimer": "Automated clearance based on a fixed policy and the supplied description. CLEAR is not a guarantee of safety; the packet attests the decision, not the outcome.",
        "cached": False,
    }
    return JSONResponse(content=shaped)


@router.get("/agent/clearance")
async def clearance_get(
    action: str = Query(..., description="Plain-language action about to run."),
    action_type: str | None = Query(None, description="delete|deploy|send|spend|transfer|publish|email|overwrite|approve|rotate|shutdown|other"),
    amount_usd: float | None = Query(None, description="Monetary stake in USD if applicable."),
    reversible: bool | None = Query(None, description="Optional hint: is the action reversible?"),
    target: str | None = Query(None, description="Optional target (table, address, repo, recipient)."),
    context: str | None = Query(None, description="Optional extra context."),
) -> JSONResponse:
    """GET /agent/clearance — same as POST (GET for Bazaar discovery)."""
    return await preflight(PreflightRequest(action=action, action_type=action_type, amount_usd=amount_usd,
                                            reversible=reversible, target=target, context=context))


@router.get("/agent/clearance/health")
async def preflight_health() -> JSONResponse:
    from app.receipt import receipt_available
    probe = _assess(PreflightRequest(action="delete the production database", action_type="delete"))
    ok = probe["verdict"] in ("REVIEW", "BLOCK")
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "preflight", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCES[0], "reachable": True, "detail": "local engine, no upstream"},
        "self_test": {"probe_verdict": probe["verdict"], "expected_in": ["REVIEW", "BLOCK"]},
        "receipt_signing": receipt_available(),
        "policy_version": POLICY_VERSION})
