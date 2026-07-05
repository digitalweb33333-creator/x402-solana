"""LOT 8 #5 — Market Intelligence ($0.10).

Synthesized multi-source market report in ONE call, built for research/trading agents.
Reuses the analysis-report engine (web_signals.gather + Claude Haiku) and fuses our own
internal market data: cross-exchange perp derivatives (derivatives_radar), directional
signal fusion (signal_fusion) and the euro-area macro snapshot (macro_snapshot).

Differentiator vs Messari ($0.10, the anchor of this slot; cf RAPPORT-BENCHMARK-12):
crypto derivatives + directional signals + a macro/EU lens combined in one report.
Separate endpoint (validated), NOT a mode of /agent/analysis-report.

Degrades gracefully: each internal leg is wrapped, the LLM narrative falls back to a
deterministic summary. Only if EVERY source fails does it 502 (agent not charged).
Sources: Binance/Bybit/OKX/Hyperliquid + ECB SDMX + public web (Jina) + Claude Haiku.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.llm import compose, llm_available
from app.routers import derivatives_radar, macro_snapshot, signal_fusion
from app.sources import web_signals
from app.sources.http_util import TTLCache, utc_now
from app.verdict import freshness, now_iso

router = APIRouter()

SOURCES_LABEL = [
    "Binance/Bybit/OKX/Hyperliquid perp derivatives (funding, OI, long/short)",
    "Cross-exchange signal fusion (funding/crowding/regime/lead-lag)",
    "European Central Bank Data Portal SDMX (macro: inflation, rates, unemployment, FX)",
    "Public web signals via Jina Reader (keyless)",
    "Claude Haiku structured synthesis",
]
_FOCI = {"crypto", "macro", "all"}
_DEPTHS = {"quick", "deep"}
_MACRO_INDICATORS = ["inflation_hicp", "core_inflation", "deposit_facility_rate", "main_refi_rate", "unemployment", "fx_usd", "fx_gbp"]
_cache = TTLCache(300)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line market headline."},
        "market_summary": {"type": "string", "description": "3-5 sentence synthesis across the provided crypto + macro facts."},
        "crypto_outlook": {"type": "object", "properties": {
            "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral", "mixed"]},
            "notes": {"type": "string"}}, "required": ["bias", "notes"], "additionalProperties": False},
        "macro_outlook": {"type": "object", "properties": {
            "stance": {"type": "string", "enum": ["easing", "tightening", "neutral", "mixed"]},
            "notes": {"type": "string"}}, "required": ["stance", "notes"], "additionalProperties": False},
        "key_signals": {"type": "array", "items": {"type": "object", "properties": {
            "signal": {"type": "string"}, "implication": {"type": "string"}},
            "required": ["signal", "implication"], "additionalProperties": False}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "opportunities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "market_summary", "crypto_outlook", "macro_outlook", "key_signals", "risks", "opportunities"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a markets strategist writing a concise, decision-useful market intelligence report for an autonomous "
    "agent. Use ONLY the structured facts provided (perp funding/OI, fused directional signals, ECB macro values, "
    "and public web snippets). Never invent numbers. Tie the crypto bias to funding/crowding/signal facts and the "
    "macro stance to ECB rates/inflation. Keep it calibrated and specific; if a section's facts are missing, say so."
)


async def _safe(coro) -> tuple[Any, str | None]:
    try:
        return await coro, None
    except HTTPException as exc:
        return None, f"http_{exc.status_code}"
    except Exception as exc:
        return None, type(exc).__name__


def _heuristic(facts: dict) -> dict[str, Any]:
    derivs = facts.get("derivatives") or {}
    sigs = facts.get("signals") or {}
    votes = [v.get("verdict") for v in sigs.values() if isinstance(v, dict)]
    longs, shorts = votes.count("LONG"), votes.count("SHORT")
    bias = "bullish" if longs > shorts else ("bearish" if shorts > longs else "neutral")
    return {
        "headline": "Automated market snapshot (LLM synthesis unavailable).",
        "market_summary": f"Fused signals lean {bias} ({longs} long / {shorts} short across tracked symbols). "
                          f"Derivatives and macro facts are attached for direct inspection.",
        "crypto_outlook": {"bias": bias, "notes": "Derived from fused directional signal votes only."},
        "macro_outlook": {"stance": "neutral", "notes": "See attached ECB macro values; no LLM interpretation."},
        "key_signals": [{"signal": f"{s} fused signal", "implication": (v.get("verdict") if isinstance(v, dict) else str(v))}
                        for s, v in list(sigs.items())[:4]],
        "risks": ["LLM synthesis unavailable — interpret the attached facts directly."],
        "opportunities": [],
    }


async def build_intel(focus: str, depth: str) -> dict[str, Any]:
    f = (focus or "all").strip().lower()
    d = (depth or "quick").strip().lower()
    if f not in _FOCI:
        raise HTTPException(status_code=400, detail={"code": "BAD_FOCUS", "message": "'focus' must be one of: crypto, macro, all."})
    if d not in _DEPTHS:
        raise HTTPException(status_code=400, detail={"code": "BAD_DEPTH", "message": "'depth' must be one of: quick, deep."})

    key = f"{f}|{d}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    symbols = ["BTC", "ETH"] + (["SOL"] if d == "deep" else [])
    want_crypto = f in ("crypto", "all")
    want_macro = f in ("macro", "all")

    tasks: dict[str, Any] = {}
    if want_crypto:
        tasks["derivatives"] = asyncio.gather(*[_safe(derivatives_radar.radar(s)) for s in symbols])
        tasks["signals"] = asyncio.gather(*[_safe(signal_fusion.fuse(s, "4h")) for s in symbols])
    if want_macro:
        tasks["macro"] = _safe(macro_snapshot.snapshot(_MACRO_INDICATORS, "U2"))
    web_queries = []
    if want_crypto:
        web_queries.append("crypto market outlook BTC ETH funding rates today")
    if want_macro:
        web_queries.append("euro area ECB inflation interest rate outlook")
    tasks["web"] = web_signals.gather(web_queries, per_query=5 if d == "quick" else 7, content_chars=3000) if web_queries else None

    derivatives, signals, macro, web = {}, {}, None, {"any_ok": False, "results": [], "markdown_blocks": [], "sources_ok": []}
    any_ok = False
    if want_crypto:
        dv = await tasks["derivatives"]
        derivatives = {s: (res if err is None else {"error": err}) for s, (res, err) in zip(symbols, dv)}
        sg = await tasks["signals"]
        signals = {s: (res if err is None else {"error": err}) for s, (res, err) in zip(symbols, sg)}
        any_ok = any(err is None for _, err in dv) or any(err is None for _, err in sg) or any_ok
    if want_macro:
        macro_res, macro_err = await tasks["macro"]
        macro = macro_res if macro_err is None else {"error": macro_err}
        any_ok = any_ok or macro_err is None
    if tasks["web"] is not None:
        web = await tasks["web"]
        any_ok = any_ok or web.get("any_ok", False)

    if not any_ok:
        raise HTTPException(status_code=502, detail={"code": "ALL_SOURCES_DOWN", "message": "No market source responded; not charged."})

    facts = {"focus": f, "derivatives": derivatives, "signals": signals, "macro": macro}

    mode = "llm"
    report = None
    llm_err = None
    if llm_available():
        web_md = "\n\n".join(b.get("markdown", "")[:1500] for b in web.get("markdown_blocks", []))[:6000]
        user = (f"FOCUS: {f}\nDEPTH: {d}\n\nCRYPTO DERIVATIVES:\n{derivatives}\n\nFUSED SIGNALS:\n{signals}\n\n"
                f"ECB MACRO:\n{macro}\n\nWEB SNIPPETS:\n{web_md or '(none)'}")
        report, llm_err = await compose(system=_SYSTEM, user=user, schema=_SCHEMA,
                                        tool_description="Emit the market intelligence report.", max_tokens=1600)
    if report is None:
        mode = "heuristic"
        report = _heuristic(facts)

    shaped = {
        "query": {"focus": f, "depth": d, "symbols": symbols},
        "report": {**report, "mode": mode},
        "data": {"derivatives": derivatives, "signals": signals, "macro": macro},
        "provenance": {"sources": web.get("results", [])[:10], "internal": SOURCES_LABEL[:3], "synthesis": "Claude Haiku" if mode == "llm" else "deterministic"},
        "data_freshness": freshness(now_iso(), deterministic=(mode == "heuristic"), sources=SOURCES_LABEL,
                                    extra={"mode": mode, "live_web": web.get("any_ok", False)}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Synthesis ran in heuristic mode ({llm_err or 'no key'})."},
        "source": "Multi-source: perp derivatives + signal fusion + ECB macro + web + Claude Haiku",
        "timestamp": utc_now(),
        "disclaimer": "Synthesized market intelligence from public market data and web signals. Informational, not investment advice.",
        "cached": False,
    }
    _cache.set(key, shaped)
    return shaped


@router.get("/market/intel")
async def market_intel(
    focus: str = Query("all", description="crypto | macro | all (default all)."),
    depth: str = Query("quick", description="quick | deep (deep adds SOL + more web signals)."),
) -> JSONResponse:
    """GET /market/intel — AI market intelligence report: crypto derivatives + fused signals + ECB macro synthesized in one call."""
    return JSONResponse(content=await build_intel(focus, depth))


@router.get("/market/intel/health")
async def market_intel_health() -> JSONResponse:
    return JSONResponse(status_code=200, content={
        "endpoint": "market-intel", "status": "ok",
        "composition": ["/crypto/derivatives-radar", "/crypto/signal-fusion", "/macro/snapshot", "web signals"],
        "llm_configured": llm_available(),
        "degrades_to": "heuristic synthesis if LLM unavailable; 502 only if every source is down (never 500)"})
