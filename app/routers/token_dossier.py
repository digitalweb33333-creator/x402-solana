"""LOT 8 #4 — Token Dossier ($0.10), premium tier of token-safety.

Full degen dossier for a token: the normalized safety score PLUS the detail a bare
safety check omits — top holders and concentration, liquidity/FDV/volume/pool-age,
contract control (owner/creator/mintable/open-source), and an LLM red-flag NARRATIVE.

Differentiator vs /crypto/token-safety ($0.05): detailed holders + narrative synthesis
(cf RAPPORT-BENCHMARK-12 — otherwise it would be a duplicate). Composes existing
internal callables and their source helpers; adds a Claude-Haiku narrative that degrades
to a deterministic summary if the LLM is unavailable (never a bare 500).

Sources: GoPlus + Honeypot.is + DexScreener (EVM) / Solana RPC + DexScreener (Solana) +
Claude Haiku (narrative). No new key. TTL 5 min.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.llm import compose, llm_available
from app.routers import solana_token_safety, token_safety
from app.sources.http_util import TTLCache, utc_now
from app.verdict import freshness, now_iso

router = APIRouter()

SOURCES_EVM = ["GoPlus Security", "Honeypot.is", "DexScreener", "Claude Haiku (narrative)"]
SOURCES_SOL = ["Solana RPC", "DexScreener", "Claude Haiku (narrative)"]
_SOL_CHAINS = {"solana", "sol"}
_cache = TTLCache(300)

_NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence plain-language read on this token's risk."},
        "red_flags": {"type": "array", "description": "Prioritized concrete red flags from the provided facts.", "items": {
            "type": "object",
            "properties": {
                "flag": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "explanation": {"type": "string"},
            },
            "required": ["flag", "severity", "explanation"],
            "additionalProperties": False,
        }},
        "bottom_line": {"type": "string", "description": "One-sentence actionable takeaway for a trading agent."},
    },
    "required": ["summary", "red_flags", "bottom_line"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a crypto token risk analyst writing a concise dossier narrative for an autonomous trading agent. "
    "Use ONLY the structured facts provided (safety score, flags, holders, liquidity, contract control). Never "
    "invent numbers or addresses. Be calibrated: high liquidity and renounced control lower risk; honeypot, high "
    "tax, mint authority, or extreme holder concentration raise it. If facts are thin, say so and keep red_flags short."
)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _heuristic_narrative(facts: dict, flags: list[str]) -> dict[str, Any]:
    sev = "critical" if facts.get("honeypot") else ("high" if facts.get("safety_score", 100) < 50 else
          ("medium" if facts.get("safety_score", 100) < 80 else "low"))
    red = [{"flag": f, "severity": sev, "explanation": "Detected by automated checks."} for f in flags[:6]]
    return {
        "summary": f"Automated dossier (LLM narrative unavailable). Safety score {facts.get('safety_score')}/100"
                   f"{'; flagged as a honeypot' if facts.get('honeypot') else ''}."
                   f" Liquidity ${_num(facts.get('liquidity_usd')) or 0:,.0f}.",
        "red_flags": red,
        "bottom_line": "Review the flags and concentration before trading; re-run with narrative enabled for detail.",
    }


async def _evm_dossier(token: str, chain: str) -> dict[str, Any]:
    chain_id = token_safety.CHAIN_IDS.get(chain)
    safety = await token_safety.assess_token(token, chain)  # raises 400/502 as needed
    gp, _ = await token_safety._goplus(chain_id, token)
    dex, _ = await token_safety._dexscreener(token)
    gp = gp or {}
    dex = dex or {}

    holders_raw = gp.get("holders") or []
    top_holders = [{"address": h.get("address"), "percent": _num(h.get("percent")),
                    "is_contract": str(h.get("is_contract")) == "1", "tag": h.get("tag") or None}
                   for h in holders_raw[:10]]
    top10_share = sum((_num(h.get("percent")) or 0.0) for h in holders_raw[:10])
    pair_created = _num(dex.get("pairCreatedAt"))
    import time as _t
    pool_age_hours = round((_t.time() - pair_created / 1000.0) / 3600.0, 1) if pair_created else None

    facts = {
        "safety_score": safety.get("safety_score"), "honeypot": safety.get("honeypot"),
        "buy_tax_pct": safety.get("buy_tax_pct"), "sell_tax_pct": safety.get("sell_tax_pct"),
        "liquidity_usd": (safety.get("market") or {}).get("liquidity_usd"),
    }
    return {
        "kind": "evm",
        "safety": {"score": safety.get("safety_score"), "rating": safety.get("rating"),
                   "honeypot": safety.get("honeypot"), "buy_tax_pct": safety.get("buy_tax_pct"),
                   "sell_tax_pct": safety.get("sell_tax_pct"), "flags": safety.get("flags") or []},
        "holders": {"count": gp.get("holder_count"), "top": top_holders,
                    "top10_share_pct": round(top10_share * 100, 2) if holders_raw else None},
        "liquidity": {"liquidity_usd": (safety.get("market") or {}).get("liquidity_usd"),
                      "fdv_usd": _num(dex.get("fdv")) or _num(dex.get("marketCap")),
                      "volume_24h_usd": (safety.get("market") or {}).get("volume_24h_usd"),
                      "price_usd": (safety.get("market") or {}).get("price_usd"),
                      "price_change_24h_pct": _num((dex.get("priceChange") or {}).get("h24")),
                      "pool_age_hours": pool_age_hours, "dex": (safety.get("market") or {}).get("dex"),
                      "symbol": (safety.get("market") or {}).get("symbol")},
        "contract": {"open_source": str(gp.get("is_open_source")) == "1" if gp.get("is_open_source") is not None else None,
                     "mintable": str(gp.get("is_mintable")) == "1" if gp.get("is_mintable") is not None else None,
                     "owner_address": gp.get("owner_address") or None,
                     "creator_address": gp.get("creator_address") or None,
                     "is_proxy": str(gp.get("is_proxy")) == "1" if gp.get("is_proxy") is not None else None,
                     "can_take_back_ownership": str(gp.get("can_take_back_ownership")) == "1" if gp.get("can_take_back_ownership") is not None else None},
        "_facts": facts, "_flags": safety.get("flags") or [], "_sources": SOURCES_EVM,
    }


async def _sol_dossier(mint: str) -> dict[str, Any]:
    data = await solana_token_safety.assess(mint, deep=True)  # raises 400/502
    market = data.get("market") or {}
    conc = data.get("concentration") or {}
    all_flags = [f["label"] for f in (data.get("static_flags") or []) + (data.get("behavioral_flags") or [])]
    facts = {"safety_score": data.get("score"), "honeypot": data.get("verdict") == "CRITICAL",
             "liquidity_usd": market.get("liquidity_usd")}
    return {
        "kind": "solana",
        "safety": {"score": data.get("score"), "rating": data.get("verdict"),
                   "honeypot": data.get("verdict") == "CRITICAL", "flags": all_flags},
        "holders": {"top1_share": conc.get("top1_share"), "top5_share": conc.get("top5_share"),
                    "note": conc.get("note")},
        "liquidity": {"liquidity_usd": market.get("liquidity_usd"), "fdv_usd": market.get("fdv_usd"),
                      "volume_24h_usd": market.get("volume_24h_usd"), "price_change_24h_pct": market.get("price_change_24h_pct"),
                      "pool_age_hours": market.get("pool_age_hours"), "dex": market.get("dex"), "symbol": market.get("symbol")},
        "contract": {"mint_authority": (data.get("mint_info") or {}).get("mint_authority"),
                     "freeze_authority": (data.get("mint_info") or {}).get("freeze_authority"),
                     "decimals": (data.get("mint_info") or {}).get("decimals"),
                     "is_token_2022": (data.get("mint_info") or {}).get("is_token_2022")},
        "_facts": facts, "_flags": all_flags, "_sources": SOURCES_SOL,
    }


async def build_dossier(token: str, chain: str) -> dict[str, Any]:
    c = (chain or "base").strip().lower()
    tok = (token or "").strip()
    if not tok:
        raise HTTPException(status_code=400, detail={"code": "TOKEN_REQUIRED", "message": "'token' is required."})

    key = f"{c}|{tok.lower()}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    if c in _SOL_CHAINS:
        core = await _sol_dossier(tok)
    else:
        core = await _evm_dossier(tok, c)

    # LLM narrative from assembled facts
    mode = "llm"
    narrative = None
    llm_err = None
    if llm_available():
        facts_json = {k: v for k, v in core.items() if not k.startswith("_")}
        user = f"TOKEN: {tok} (chain={c})\n\nSTRUCTURED FACTS:\n{facts_json}"
        narrative, llm_err = await compose(system=_SYSTEM, user=user, schema=_NARRATIVE_SCHEMA,
                                           tool_description="Emit the token dossier narrative.", max_tokens=1024)
    if narrative is None:
        mode = "heuristic"
        narrative = _heuristic_narrative(core["_facts"], core["_flags"])

    shaped = {
        "query": {"token": tok, "chain": c},
        "safety": core["safety"],
        "holders": core["holders"],
        "liquidity": core["liquidity"],
        "contract": core["contract"],
        "narrative": {**narrative, "mode": mode},
        "data_freshness": freshness(now_iso(), deterministic=False, sources=core["_sources"],
                                    extra={"narrative_mode": mode, "chain_kind": core["kind"]}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Narrative ran in heuristic mode ({llm_err or 'no key'})."},
        "source": " + ".join(core["_sources"]),
        "timestamp": utc_now(),
        "disclaimer": "Automated token dossier (safety + holders + liquidity + narrative). Heuristic, not financial advice. Always DYOR.",
        "cached": False,
    }
    _cache.set(key, shaped)
    return shaped


@router.get("/crypto/token-dossier")
async def token_dossier(
    token: str = Query(..., description="Token contract (EVM) or SPL mint (Solana)."),
    chain: str = Query("base", description="base | ethereum | bsc | polygon | arbitrum | optimism | avalanche | solana"),
) -> JSONResponse:
    """GET /crypto/token-dossier — full token dossier: safety score + top holders + liquidity + contract control + LLM red-flag narrative."""
    return JSONResponse(content=await build_dossier(token, chain))


@router.get("/crypto/token-dossier/health")
async def token_dossier_health() -> JSONResponse:
    from app.sources.http_util import client, get_json
    cl = await client("goplus")
    data, err = await get_json(cl, "https://api.gopluslabs.io/api/v1/supported_chains")
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "token-dossier", "status": "ok" if ok else "degraded",
        "upstream": {"reachable": ok, "detail": err or "HTTP 200", "llm_configured": llm_available()},
        "degrades_to": "heuristic narrative if LLM unavailable (never 500)", "cache_entries": len(_cache)})
