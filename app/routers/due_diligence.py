"""Endpoint #1 (LOT 9) — Due-Diligence Dossier (composé via Haiku, $0.50).

Une cible (entité / projet / contrepartie / domaine) → un DOSSIER de risque structuré :
synthèse + signaux détectés + sources + score 0-100 + verdict {GO, CAUTION, STOP} +
recommandation + as_of + reçu signé. PAS un lookup : un appel REMPLACE 3-4 recherches +
la synthèse qu'un agent aurait dû fabriquer lui-même.

Pipeline : (1) collecte de signaux publics EN PARALLÈLE (Jina keyless), (2) synthèse
déterministe par Claude Haiku (température basse, JSON strict schématisé), (3) reçu signé
Ed25519. Si la clé Haiku manque OU si la synthèse échoue → dégradation HEURISTIQUE propre
(jamais de 500, champ `mode` signalant le fallback).

Benchmark (cf BILAN) : x402-secure / RugGuard / Kerdos couvrent le risque crypto-wallet
ou la market-intel. L'edge = un dossier COMPOSÉ multi-signaux sur tout type de cible
(pas seulement un wallet), synthèse LLM bornée + reçu signé pour l'audit.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.llm import compose, llm_available
from app.receipt import sign_receipt
from app.sources import web_signals
from app.sources.http_util import TTLCache
from app.verdict import freshness, now_iso, reason

router = APIRouter()

SOURCES_LABEL = ["Public web signals via Jina Reader (keyless)", "Claude Haiku structured synthesis"]
_cache = TTLCache(900)  # 15 min

_RISK_KW = {
    "critical": re.compile(r"\b(?:rug\s*pull|ponzi|sanction(?:ed|s)?|indicted|fraud(?:ulent)?|stole(?:n)?|exit\s*scam|money\s*launder)\b", re.I),
    "high": re.compile(r"\b(?:scam|hack(?:ed)?|exploit|lawsuit|sued|breach|phishing|banned|blacklist|insolvenc|bankrupt|rug)\b", re.I),
    "medium": re.compile(r"\b(?:complaint|warning|investigat|controvers|delisted|downtime|outage|refund|chargeback|dispute)\b", re.I),
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence risk synthesis of the target."},
        "signals": {"type": "array", "description": "Concrete risk/assurance signals found.", "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["legal", "financial", "reputation", "security", "operational", "identity", "sanctions", "other"]},
                "finding": {"type": "string"},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
            },
            "required": ["category", "finding", "severity"],
            "additionalProperties": False,
        }},
        "risk_score": {"type": "integer", "description": "0 (no risk) to 100 (severe risk)."},
        "verdict": {"type": "string", "enum": ["GO", "CAUTION", "STOP"]},
        "recommendation": {"type": "string", "description": "One actionable next step for the agent."},
        "confidence": {"type": "number", "description": "0.0-1.0 confidence given the available evidence."},
    },
    "required": ["summary", "signals", "risk_score", "verdict", "recommendation", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a due-diligence analyst building a risk dossier for an autonomous AI agent that is "
    "about to engage with a target (a company, project, counterparty, or domain). "
    "Use ONLY the public web signals provided plus widely-known facts; never invent specifics, URLs, "
    "or numbers. Be calibrated: absence of negative signals is NOT proof of safety — say so and lower "
    "confidence. Map verdict strictly: GO = no material risk signals and the target looks legitimate; "
    "CAUTION = some risk signals or thin/ambiguous evidence; STOP = strong negative signals (fraud, "
    "sanctions, hacks, legal action) or the target cannot be verified at all. risk_score must align with "
    "the verdict (GO<35, CAUTION 35-69, STOP>=70). Keep findings specific and sourced to the provided text."
)


class DDRequest(BaseModel):
    target: str = Field(..., description="Entity / project / counterparty / domain to vet, e.g. 'Acme DeFi Labs' or 'acme-defi.xyz'.")
    target_type: str | None = Field(None, description="Optional hint: 'entity' | 'project' | 'counterparty' | 'domain' | 'wallet'.")
    context: str | None = Field(None, description="Optional context, e.g. why you're engaging or what you're about to do with them.")


def _heuristic(target: str, signals_text: str) -> dict[str, Any]:
    """Fallback déterministe quand le LLM est indisponible."""
    found: list[dict] = []
    worst = 0
    sev_rank = {"medium": 1, "high": 2, "critical": 3}
    for sev, rx in _RISK_KW.items():
        m = rx.search(signals_text or "")
        if m:
            found.append({"category": "reputation", "finding": f"Public signal matched risk term '{m.group(0)}'.", "severity": sev})
            worst = max(worst, sev_rank[sev])
    if worst >= 3:
        verdict, score = "STOP", 82
    elif worst == 2:
        verdict, score = "STOP", 72
    elif worst == 1:
        verdict, score = "CAUTION", 50
    elif signals_text:
        verdict, score = "CAUTION", 38
        found.append({"category": "identity", "finding": "Public mentions found but no clear risk or assurance signal isolated without LLM synthesis.", "severity": "low"})
    else:
        verdict, score = "CAUTION", 45
        found.append({"category": "identity", "finding": "No public signals retrieved; target could not be verified.", "severity": "medium"})
    return {
        "summary": f"Heuristic dossier for '{target}' (LLM synthesis unavailable). "
                   f"{'Negative risk terms detected in public signals.' if worst else 'No strong risk terms isolated; evidence is thin.'}",
        "signals": found,
        "risk_score": score,
        "verdict": verdict,
        "recommendation": "Re-run with LLM synthesis enabled, or perform manual review before engaging.",
        "confidence": 0.4,
    }


@router.post("/agent/due-diligence")
async def due_diligence(req: DDRequest) -> JSONResponse:
    """POST /agent/due-diligence — composed risk dossier (summary + signals + score + GO/CAUTION/STOP + signed receipt). One call replaces 3-4 lookups."""
    target = (req.target or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_TARGET", "message": "'target' must be a non-empty string."})
    if len(target) > 200:
        raise HTTPException(status_code=400, detail={"code": "TARGET_TOO_LONG", "message": "'target' must be <= 200 chars."})

    cache_key = f"{target.lower()}|{(req.target_type or '').lower()}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return JSONResponse(content={**cached, "cached": True})

    # (1) signaux publics EN PARALLÈLE
    queries = [target, f"{target} scam fraud lawsuit hack sanction complaint review"]
    sig = await web_signals.gather(queries, per_query=5, content_chars=3500)
    sources = sig["results"][:12]
    signals_text = "\n\n".join(f"### {b['query']}\n{b['markdown']}" for b in sig["markdown_blocks"] if b.get("markdown"))

    mode = "llm"
    dossier: dict[str, Any] | None = None
    llm_error = None
    if llm_available():
        user = (
            f"TARGET: {target}\n"
            f"TARGET_TYPE: {req.target_type or 'unspecified'}\n"
            f"ENGAGEMENT_CONTEXT: {req.context or 'none provided'}\n\n"
            f"PUBLIC WEB SIGNALS (may be empty or noisy):\n{signals_text or '(no public signals retrieved)'}\n\n"
            f"RESULT LINKS:\n" + "\n".join(f"- {r['title']} ({r['url']})" for r in sources[:8])
        )
        dossier, llm_error = await compose(system=_SYSTEM, user=user, schema=_OUTPUT_SCHEMA,
                                           tool_description="Emit the due-diligence dossier.", max_tokens=1536)
    if dossier is None:
        mode = "heuristic"
        dossier = _heuristic(target, signals_text)

    # Garde-fous de cohérence verdict/score (le schéma garantit les types, pas la logique).
    score = max(0, min(100, int(dossier.get("risk_score", 50))))
    verdict = dossier.get("verdict") if dossier.get("verdict") in ("GO", "CAUTION", "STOP") else "CAUTION"
    reasons = [reason(f"SIGNAL_{i+1}", f"[{s.get('severity','?')}/{s.get('category','?')}] {s.get('finding','')[:120]}",
                      {"info": 0.0, "low": 0.1, "medium": 0.3, "high": 0.6, "critical": 0.9}.get(s.get("severity"), 0.2))
               for i, s in enumerate(dossier.get("signals", [])[:8])]

    receipt = sign_receipt({
        "kind": "due_diligence_dossier",
        "target": target,
        "verdict": verdict,
        "risk_score": score,
        "signal_count": len(dossier.get("signals", [])),
        "mode": mode,
        "as_of": now_iso(),
    })

    shaped = {
        "verdict": verdict,
        "confidence": round(float(dossier.get("confidence", 0.5)), 3),
        "risk_score": score,
        "summary": dossier.get("summary", ""),
        "signals": dossier.get("signals", []),
        "reasons": reasons,
        "recommendation": dossier.get("recommendation", ""),
        "query": {"target": target, "target_type": req.target_type},
        "sources": sources,
        "signed_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=(mode == "heuristic"), sources=SOURCES_LABEL,
                                    extra={"mode": mode, "live_signals": sig["any_ok"], "sources_ok": sig["sources_ok"]}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Synthesis ran in heuristic mode ({llm_error or 'no key'})."},
        "timestamp": now_iso(),
        "disclaimer": "Risk dossier composed from public web signals; signals may be incomplete, stale, or about a different entity sharing the name. Not legal/financial advice or a compliance opinion.",
        "cached": False,
    }
    _cache.set(cache_key, shaped)
    return JSONResponse(content=shaped)


@router.get("/agent/due-diligence")
async def due_diligence_get(
    target: str = Query(..., description="Entity / project / counterparty / domain to vet."),
    target_type: str | None = Query(None, description="entity | project | counterparty | domain | wallet"),
    context: str | None = Query(None, description="Optional engagement context."),
) -> JSONResponse:
    """GET /agent/due-diligence — same as POST (GET for Bazaar discovery)."""
    return await due_diligence(DDRequest(target=target, target_type=target_type, context=context))


@router.get("/agent/due-diligence/health")
async def due_diligence_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "due-diligence", "status": "ok",
        "upstream": {"llm_configured": llm_available(), "signal_source": "Jina r.jina.ai (keyless)"},
        "receipt_signing": receipt_available(),
        "degrades_to": "heuristic mode if LLM/signals unavailable (never 500)"})
