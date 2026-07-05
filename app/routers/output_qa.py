"""Endpoint #4 (LOT 9) — Agent Output QA + rewrite ($0.10).

Une sortie d'agent (email / post / réponse client) → score multi-critères à VOCABULAIRE
FERMÉ (clarity, spam_safety, tone, length, personalization, cta, compliance — chacun 0-100,
plus haut = meilleur) + overall_score + rating {poor, fair, good, excellent} + top
suggestions + version RÉÉCRITE améliorée + as_of.

La réécriture nécessite le LLM (Haiku) ; le scoring tombe en heuristique déterministe si la
clé manque (jamais de 500 ; `mode` signale le fallback ; improved_version = original).
Pas de reçu signé (réservé aux premium #1/#2/#3).
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.llm import compose, llm_available
from app.sources.http_util import TTLCache
from app.verdict import freshness, now_iso

router = APIRouter()

SOURCES_LABEL = ["Claude Haiku rubric scoring + rewrite"]
_cache = TTLCache(600)
MAX_LEN = 8000

_CRITERIA = ["clarity", "spam_safety", "tone", "length", "personalization", "cta", "compliance"]

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer", "description": "0-100 overall quality (higher is better)."},
        "rating": {"type": "string", "enum": ["poor", "fair", "good", "excellent"]},
        "criteria": {
            "type": "object",
            "description": "Each 0-100, higher is better. spam_safety: 100 = not spammy.",
            "properties": {c: {"type": "integer"} for c in _CRITERIA},
            "required": _CRITERIA,
            "additionalProperties": False,
        },
        "suggestions": {"type": "array", "items": {"type": "string"}, "description": "3-5 concrete improvement suggestions."},
        "improved_version": {"type": "string", "description": "The rewritten, improved output preserving the original intent."},
    },
    "required": ["overall_score", "rating", "criteria", "suggestions", "improved_version"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are an output-QA reviewer for an autonomous AI agent's outbound text (email, social post, or "
    "customer reply). Score each criterion 0-100 where HIGHER IS BETTER; for spam_safety, 100 means NOT "
    "spammy. Criteria: clarity, spam_safety, tone, length (appropriateness for the format), personalization, "
    "cta (clarity/presence of a call to action), compliance (no false claims, no risky/misleading content). "
    "overall_score must reflect the criteria. rating: poor<40, fair 40-59, good 60-79, excellent>=80. "
    "Give 3-5 specific, actionable suggestions. Then provide improved_version: a tightened rewrite that keeps "
    "the original intent and audience, fixes the weak criteria, and is ready to send."
)

_SPAM_WORDS = re.compile(r"\b(?:free|guarantee[d]?|act\s+now|limited\s+time|click\s+here|buy\s+now|cash|winner|congratulations|risk[-\s]?free|100%|urgent|exclusive\s+deal|cheap)\b", re.I)
_CTA = re.compile(r"\b(?:reply|book|schedule|call|sign\s+up|register|download|let\s+me\s+know|get\s+started|learn\s+more|click|visit|contact)\b", re.I)
_PERSONAL = re.compile(r"\b(?:you|your|hi\s+\w+|hello\s+\w+|dear\s+\w+)\b", re.I)


def _heuristic(text: str, fmt: str) -> dict[str, Any]:
    words = len(text.split())
    caps = sum(1 for ch in text if ch.isupper())
    caps_ratio = caps / max(1, len(text))
    exclaims = text.count("!")
    spam_hits = len(_SPAM_WORDS.findall(text))

    spam_safety = max(0, 100 - spam_hits * 18 - int(caps_ratio > 0.3) * 25 - min(20, exclaims * 7))
    clarity = max(0, 100 - max(0, words - 220) // 6 - exclaims * 3)
    length = 100 if (15 <= words <= 200) else max(20, 100 - abs(words - 110) // 3)
    personalization = min(100, 40 + len(_PERSONAL.findall(text)) * 12)
    cta = 80 if _CTA.search(text) else 30
    tone = max(0, 90 - exclaims * 8 - int(caps_ratio > 0.3) * 30)
    compliance = max(0, 100 - spam_hits * 10)
    crit = {"clarity": clarity, "spam_safety": spam_safety, "tone": tone, "length": length,
            "personalization": personalization, "cta": cta, "compliance": compliance}
    overall = int(round(sum(crit.values()) / len(crit)))
    rating = "excellent" if overall >= 80 else "good" if overall >= 60 else "fair" if overall >= 40 else "poor"
    suggestions = []
    if spam_safety < 70:
        suggestions.append("Remove spammy/hype words and excessive caps or exclamation marks.")
    if cta < 60:
        suggestions.append("Add a single, clear call to action.")
    if personalization < 60:
        suggestions.append("Personalize the opening (recipient name, specific context).")
    if length != 100:
        suggestions.append("Adjust length to the format (aim ~40-150 words for an email/reply).")
    if not suggestions:
        suggestions.append("Solid output; minor polishing only.")
    return {"overall_score": overall, "rating": rating, "criteria": crit, "suggestions": suggestions,
            "improved_version": text}  # pas de réécriture sans LLM


class QARequest(BaseModel):
    output: str = Field(..., description="The agent's outbound text to review (email, post, or customer reply).")
    format: str | None = Field(None, description="Optional hint: 'email' | 'social_post' | 'customer_reply' | 'other'.")
    goal: str | None = Field(None, description="Optional intended goal, e.g. 'book a demo', 'resolve a complaint'.")


@router.post("/agent/output-qa")
async def output_qa(req: QARequest) -> JSONResponse:
    """POST /agent/output-qa — multi-criteria QA score (closed vocab) + suggestions + improved rewrite of an agent's outbound text."""
    text = (req.output or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_OUTPUT", "message": "'output' must be a non-empty string."})
    truncated = len(text) > MAX_LEN
    scanned = text[:MAX_LEN] if truncated else text
    fmt = req.format or "other"

    cache_key = f"{hash(scanned)}|{fmt}|{req.goal or ''}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return JSONResponse(content={**cached, "cached": True})

    mode = "llm"
    result: dict[str, Any] | None = None
    llm_error = None
    if llm_available():
        user = (f"FORMAT: {fmt}\nGOAL: {req.goal or 'unspecified'}\n\nAGENT OUTPUT TO REVIEW:\n\"\"\"\n{scanned}\n\"\"\"")
        result, llm_error = await compose(system=_SYSTEM, user=user, schema=_OUTPUT_SCHEMA,
                                          tool_description="Emit the QA scorecard and improved rewrite.", max_tokens=1536)
    if result is None:
        mode = "heuristic"
        result = _heuristic(scanned, fmt)

    crit = {c: max(0, min(100, int(result.get("criteria", {}).get(c, 50)))) for c in _CRITERIA}
    overall = max(0, min(100, int(result.get("overall_score", 50))))
    rating = result.get("rating") if result.get("rating") in ("poor", "fair", "good", "excellent") else "fair"

    shaped = {
        "rating": rating,
        "overall_score": overall,
        "criteria": crit,
        "criteria_legend": "Each 0-100, higher is better. spam_safety: 100 = not spammy.",
        "suggestions": result.get("suggestions", [])[:6],
        "improved_version": result.get("improved_version", scanned),
        "query": {"format": req.format, "goal": req.goal, "output_length": len(text), "truncated": truncated},
        "data_freshness": freshness(now_iso(), deterministic=(mode == "heuristic"), sources=SOURCES_LABEL,
                                    extra={"mode": mode}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Scoring ran in heuristic mode ({llm_error or 'no key'}); rewrite unavailable."},
        "timestamp": now_iso(),
        "disclaimer": "Automated rubric QA; scores are heuristic guidance, not a guarantee of deliverability or compliance.",
        "cached": False,
    }
    _cache.set(cache_key, shaped)
    return JSONResponse(content=shaped)


@router.get("/agent/output-qa")
async def output_qa_get(
    output: str = Query(..., description="The agent's outbound text to review."),
    format: str | None = Query(None, description="email | social_post | customer_reply | other"),
    goal: str | None = Query(None, description="Optional intended goal."),
) -> JSONResponse:
    """GET /agent/output-qa — same as POST (GET for Bazaar discovery; use POST for large text)."""
    return await output_qa(QARequest(output=output, format=format, goal=goal))


@router.get("/agent/output-qa/health")
async def output_qa_health() -> JSONResponse:
    return JSONResponse(status_code=200, content={
        "endpoint": "output-qa", "status": "ok",
        "upstream": {"llm_configured": llm_available()},
        "degrades_to": "heuristic scoring (no rewrite) if LLM unavailable (never 500)"})
