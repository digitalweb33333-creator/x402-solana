"""Endpoint #2 (LOT 9) — Analysis Report (composé via Haiku, $0.25).

Un input libre (URL / entreprise / sujet) → un RAPPORT structuré à schéma FIXE :
positionnement + forces + risques + opportunités + score actionnable + rating
{STRONG, MODERATE, WEAK} + recommandation + provenance + as_of + reçu signé.

Livrable d'analyse déterministe (sections constantes). Même pipeline que #1 :
signaux publics EN PARALLÈLE → synthèse Haiku JSON strict → reçu signé, avec
dégradation heuristique propre si la clé/synthèse manque (jamais de 500).

Benchmark (cf BILAN) : pas d'équivalent direct « rapport d'analyse structuré » trouvé
sur x402scan/Bazaar ; l'edge = schéma fixe + provenance + reçu, consommable par un agent
sans re-parsing.
"""
from __future__ import annotations

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
_cache = TTLCache(900)

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence executive summary."},
        "positioning": {"type": "string", "description": "How the subject is positioned in its market/space."},
        "strengths": {"type": "array", "items": {"type": "string"}, "description": "Key strengths / positive signals."},
        "risks": {"type": "array", "items": {
            "type": "object",
            "properties": {"point": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["point", "severity"], "additionalProperties": False,
        }},
        "opportunities": {"type": "array", "items": {
            "type": "object",
            "properties": {"point": {"type": "string"}, "potential": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["point", "potential"], "additionalProperties": False,
        }},
        "score": {"type": "integer", "description": "0-100 actionable health/attractiveness score."},
        "rating": {"type": "string", "enum": ["STRONG", "MODERATE", "WEAK"]},
        "recommendation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["summary", "positioning", "strengths", "risks", "opportunities", "score", "rating", "recommendation", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a research analyst producing a structured report for an autonomous AI agent. "
    "Use ONLY the public web signals provided plus widely-known facts; never invent specifics, URLs, or "
    "numbers. Fill every section. Be calibrated and lower confidence when evidence is thin. "
    "Align rating with score: STRONG>=70, MODERATE 40-69, WEAK<40. Keep points concrete and grounded in the "
    "provided text. If the input is a URL, analyse the entity/offering behind it."
)


class ReportRequest(BaseModel):
    input: str = Field(..., description="Free input to analyse: a URL, company name, product, or subject, e.g. 'stripe.com' or 'the market for AI code review tools'.")
    focus: str | None = Field(None, description="Optional analysis focus, e.g. 'investment', 'competitive', 'partnership', 'security'.")


def _heuristic(subject: str, has_signals: bool) -> dict[str, Any]:
    return {
        "summary": f"Heuristic report for '{subject}' (LLM synthesis unavailable). "
                   f"{'Public mentions were retrieved but not synthesised.' if has_signals else 'No public signals retrieved.'}",
        "positioning": "Not determinable without LLM synthesis.",
        "strengths": [],
        "risks": [{"point": "Analysis ran in degraded mode; treat as inconclusive.", "severity": "medium"}],
        "opportunities": [],
        "score": 50,
        "rating": "MODERATE",
        "recommendation": "Re-run with LLM synthesis enabled for a full report.",
        "confidence": 0.35,
    }


@router.post("/agent/analysis-report")
async def analysis_report(req: ReportRequest) -> JSONResponse:
    """POST /agent/analysis-report — fixed-schema report (positioning + strengths + risks + opportunities + score + STRONG/MODERATE/WEAK + signed receipt)."""
    subject = (req.input or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_INPUT", "message": "'input' must be a non-empty string."})
    if len(subject) > 400:
        raise HTTPException(status_code=400, detail={"code": "INPUT_TOO_LONG", "message": "'input' must be <= 400 chars."})

    cache_key = f"{subject.lower()}|{(req.focus or '').lower()}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return JSONResponse(content={**cached, "cached": True})

    focus = req.focus or "general"
    queries = [subject, f"{subject} {focus} review analysis competitors risks"]
    sig = await web_signals.gather(queries, per_query=5, content_chars=3500)
    sources = sig["results"][:12]
    signals_text = "\n\n".join(f"### {b['query']}\n{b['markdown']}" for b in sig["markdown_blocks"] if b.get("markdown"))

    mode = "llm"
    report: dict[str, Any] | None = None
    llm_error = None
    if llm_available():
        user = (
            f"SUBJECT: {subject}\nFOCUS: {focus}\n\n"
            f"PUBLIC WEB SIGNALS (may be empty or noisy):\n{signals_text or '(no public signals retrieved)'}\n\n"
            f"RESULT LINKS:\n" + "\n".join(f"- {r['title']} ({r['url']})" for r in sources[:8])
        )
        report, llm_error = await compose(system=_SYSTEM, user=user, schema=_OUTPUT_SCHEMA,
                                          tool_description="Emit the structured analysis report.", max_tokens=1536)
    if report is None:
        mode = "heuristic"
        report = _heuristic(subject, bool(signals_text))

    score = max(0, min(100, int(report.get("score", 50))))
    rating = report.get("rating") if report.get("rating") in ("STRONG", "MODERATE", "WEAK") else "MODERATE"
    reasons = [reason(f"RISK_{i+1}", f"[{r.get('severity','?')}] {r.get('point','')[:120]}",
                      {"low": 0.1, "medium": 0.3, "high": 0.6}.get(r.get("severity"), 0.2))
               for i, r in enumerate(report.get("risks", [])[:6])]

    receipt = sign_receipt({
        "kind": "analysis_report",
        "subject": subject,
        "rating": rating,
        "score": score,
        "mode": mode,
        "as_of": now_iso(),
    })

    shaped = {
        "rating": rating,
        "score": score,
        "confidence": round(float(report.get("confidence", 0.5)), 3),
        "summary": report.get("summary", ""),
        "positioning": report.get("positioning", ""),
        "strengths": report.get("strengths", []),
        "risks": report.get("risks", []),
        "opportunities": report.get("opportunities", []),
        "reasons": reasons,
        "recommendation": report.get("recommendation", ""),
        "query": {"input": subject, "focus": req.focus},
        "provenance": {"sources": sources, "signal_source": "Jina Reader (keyless)", "synthesis": "Claude Haiku" if mode == "llm" else "heuristic fallback"},
        "signed_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=(mode == "heuristic"), sources=SOURCES_LABEL,
                                    extra={"mode": mode, "live_signals": sig["any_ok"], "sources_ok": sig["sources_ok"]}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Synthesis ran in heuristic mode ({llm_error or 'no key'})."},
        "timestamp": now_iso(),
        "disclaimer": "Structured analysis composed from public web signals; may be incomplete or stale. Not investment, legal, or professional advice.",
        "cached": False,
    }
    _cache.set(cache_key, shaped)
    return JSONResponse(content=shaped)


@router.get("/agent/analysis-report")
async def analysis_report_get(
    input: str = Query(..., description="URL, company, product or subject to analyse."),
    focus: str | None = Query(None, description="investment | competitive | partnership | security | general"),
) -> JSONResponse:
    """GET /agent/analysis-report — same as POST (GET for Bazaar discovery)."""
    return await analysis_report(ReportRequest(input=input, focus=focus))


@router.get("/agent/analysis-report/health")
async def analysis_report_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "analysis-report", "status": "ok",
        "upstream": {"llm_configured": llm_available(), "signal_source": "Jina r.jina.ai (keyless)"},
        "receipt_signing": receipt_available(),
        "degrades_to": "heuristic mode if LLM/signals unavailable (never 500)"})
