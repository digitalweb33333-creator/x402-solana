"""LOT 8 #6 — x402 Seller Audit ($0.50), the catalogue's premium report. Built last.

A deep, composed audit of an x402/ACP seller — deeper than the $1 verified badges
(cf RAPPORT-BENCHMARK-12). Combines, in one call:
  1. Identity: if given a domain, fetch its /.well-known/x402.json to enumerate advertised
     endpoints and resolve the payTo wallet; if given a wallet, use it directly.
  2. On-chain reputation: /x402/seller-trust (settlement graph, wash-trade/sybil, OFAC).
  3. Liveness: on-chain settlement recency (real), NOT home probing and NOT a cron.
  4. Web reputation + red flags: web_signals + Claude Haiku synthesis (like due-diligence).
  5. A combined TRUSTED/CAUTION/AVOID verdict + narrative + signed Ed25519 receipt.

No home probing, no scheduler (per spec). External directory uptime (402index) is attempted
best-effort and its availability is reported honestly — never faked. Degrades gracefully;
502 only if NOTHING resolves (no wallet, no discovery, no web) → agent not charged.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.llm import compose, llm_available
from app.receipt import sign_receipt
from app.routers import seller_trust
from app.sources import web_signals
from app.sources.http_util import TTLCache, client, get_json
from app.verdict import age_seconds, freshness, now_iso, reason

router = APIRouter()

SOURCES_LABEL = [
    "Seller /.well-known/x402.json discovery (advertised endpoints)",
    "Internal /x402/seller-trust (Base settlement graph + OFAC)",
    "Public web signals via Jina Reader (keyless)",
    "Claude Haiku structured synthesis",
]
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(600)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "3-5 sentence audit synthesis of the seller."},
        "reputation": {"type": "object", "properties": {
            "level": {"type": "string", "enum": ["strong", "moderate", "weak", "unverified"]},
            "notes": {"type": "string"}}, "required": ["level", "notes"], "additionalProperties": False},
        "red_flags": {"type": "array", "items": {"type": "object", "properties": {
            "flag": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "explanation": {"type": "string"}}, "required": ["flag", "severity", "explanation"], "additionalProperties": False}},
        "verdict": {"type": "string", "enum": ["TRUSTED", "CAUTION", "AVOID"]},
        "recommendation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["summary", "reputation", "red_flags", "verdict", "recommendation", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are auditing an x402 API seller (a machine-payable API merchant) for an autonomous buyer agent about to "
    "pay it. Use ONLY the provided facts: on-chain settlement metrics, advertised endpoints, and public web signals. "
    "Never invent numbers. Weigh: healthy diverse settlements and a verifiable identity raise trust; wash-trade/sybil "
    "signals, OFAC hits, no track record, or scam/complaint signals lower it. Map verdict: TRUSTED = solid on-chain "
    "record and no negative signals; CAUTION = thin/ambiguous; AVOID = sanctions, wash-trade/sybil, or scam signals. "
    "Keep red_flags specific and grounded in the facts."
)


def _harvest_discovery(doc: Any) -> tuple[list[dict], str | None]:
    """Defensively extract advertised endpoints + payTo from any x402.json shape."""
    endpoints: list[dict] = []
    pay_to: str | None = None

    def walk(node: Any) -> None:
        nonlocal pay_to
        if isinstance(node, dict):
            pt = node.get("payTo") or node.get("pay_to")
            if isinstance(pt, str) and _ADDR_RE.match(pt.strip()):
                pay_to = pay_to or pt.strip()
            if node.get("resource"):
                endpoints.append({"resource": node.get("resource"), "method": node.get("method"),
                                  "price": node.get("price") or node.get("amount"),
                                  "description": (node.get("description") or "")[:200]})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    # de-dupe endpoints by resource
    seen, uniq = set(), []
    for e in endpoints:
        r = e.get("resource")
        if r and r not in seen:
            seen.add(r)
            uniq.append(e)
    return uniq, pay_to


async def _fetch_discovery(host: str) -> tuple[dict | None, str | None]:
    c = await client("audit", timeout=12.0)
    for path in ("/.well-known/x402.json", "/.well-known/x402"):
        data, err = await get_json(c, f"https://{host}{path}")
        if data is not None:
            return data, None
    return None, err or "no_discovery"


async def _directory_uptime(host: str | None, wallet: str | None) -> dict[str, Any]:
    """Best-effort external directory uptime (402index). Honestly reports availability; never fabricates."""
    if not host and not wallet:
        return {"available": False, "source": "402index.io", "reason": "no host/wallet to query"}
    c = await client("audit", timeout=8.0)
    q = host or wallet
    data, err = await get_json(c, f"https://402index.io/api/search", params={"q": q}, attempts=1)
    if err or not isinstance(data, (dict, list)):
        return {"available": False, "source": "402index.io",
                "reason": f"public directory uptime not retrievable ({err or 'unexpected_shape'})",
                "note": "Use on-chain settlement recency below as the liveness signal."}
    return {"available": True, "source": "402index.io", "raw": data if isinstance(data, dict) else {"items": data}}


def _liveness(metrics: dict | None) -> dict[str, Any]:
    if not metrics:
        return {"available": False, "note": "No on-chain settlement metrics."}
    last = metrics.get("last_settlement") or metrics.get("first_settlement")
    age = age_seconds(last)
    active = age is not None and age < 30 * 86400
    return {"available": True, "last_settlement": last, "last_settlement_age_seconds": age,
            "active_last_30d": active, "wallet_age_days": metrics.get("wallet_age_days"),
            "note": "Liveness from on-chain settlement recency (real), not home probing."}


def _combine_verdict(trust_verdict: str | None, ofac: bool, llm_verdict: str | None) -> str:
    if ofac:
        return "AVOID"
    order = {"AVOID": 0, "CAUTION": 1, "TRUSTED": 2, None: 1}
    tv = trust_verdict if trust_verdict in ("TRUSTED", "CAUTION", "AVOID") else None
    lv = llm_verdict if llm_verdict in ("TRUSTED", "CAUTION", "AVOID") else None
    # most conservative of the two available signals
    candidates = [v for v in (tv, lv) if v is not None]
    if not candidates:
        return "CAUTION"
    return min(candidates, key=lambda v: order[v])


async def audit(seller: str, depth: str) -> dict[str, Any]:
    s = (seller or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail={"code": "SELLER_REQUIRED", "message": "'seller' (wallet 0x… or domain) is required."})
    dep = depth if depth in ("shallow", "deep") else "shallow"

    key = f"{s.lower()}|{dep}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    # --- 1. Identity resolution ---
    is_wallet = bool(_ADDR_RE.match(s))
    host = None
    wallet = s if is_wallet else None
    endpoints: list[dict] = []
    discovery_ok = False
    discovery_err = None
    if not is_wallet:
        parsed = urlparse(s if "://" in s else f"https://{s}")
        host = (parsed.netloc or parsed.path).strip("/").split("/")[0]
        doc, discovery_err = await _fetch_discovery(host)
        if doc is not None:
            discovery_ok = True
            endpoints, pt = _harvest_discovery(doc)
            wallet = wallet or pt

    # --- 2. On-chain reputation (if we have a wallet) ---
    trust = None
    trust_err = None
    if wallet and _ADDR_RE.match(wallet):
        try:
            trust = await seller_trust.assess(wallet, dep)
        except HTTPException as exc:
            trust_err = f"http_{exc.status_code}"
        except Exception as exc:
            trust_err = type(exc).__name__

    # --- 3. Liveness + external directory uptime ---
    liveness = _liveness((trust or {}).get("metrics"))
    directory = await _directory_uptime(host, wallet)

    # --- 4. Web reputation signals ---
    label = host or wallet or s
    web = await web_signals.gather([label, f"{label} x402 seller scam review reliability rug complaint"],
                                   per_query=5, content_chars=3000)
    web_sources = web["results"][:10]
    signals_text = "\n\n".join(b.get("markdown", "") for b in web["markdown_blocks"] if b.get("markdown"))[:6000]

    ofac = bool(((trust or {}).get("data_freshness") or {}).get("ofac_list_loaded") and
                any(r.get("code") == "OFAC_SANCTIONED" for r in (trust or {}).get("reasons", [])))

    if trust is None and not discovery_ok and not web["any_ok"]:
        raise HTTPException(status_code=502, detail={"code": "NOTHING_RESOLVED",
                            "message": "Could not resolve the seller on-chain, via discovery, or on the web; not charged."})

    # --- 5. LLM narrative synthesis ---
    mode = "llm"
    narrative = None
    llm_err = None
    facts = {
        "seller_input": s, "resolved_wallet": wallet, "domain": host,
        "onchain": (trust or {}).get("metrics"), "onchain_verdict": (trust or {}).get("verdict"),
        "onchain_trust_score": (trust or {}).get("trust_score"),
        "advertised_endpoints": [e.get("resource") for e in endpoints][:40],
        "liveness": liveness,
    }
    if llm_available():
        user = (f"SELLER: {s}\nRESOLVED WALLET: {wallet or 'none'}\nDOMAIN: {host or 'none'}\n\n"
                f"ON-CHAIN FACTS:\n{facts['onchain']}\nON-CHAIN VERDICT: {facts['onchain_verdict']} "
                f"(score {facts['onchain_trust_score']})\n\nADVERTISED ENDPOINTS ({len(endpoints)}): "
                f"{facts['advertised_endpoints']}\n\nLIVENESS: {liveness}\n\nWEB SIGNALS:\n{signals_text or '(none)'}")
        narrative, llm_err = await compose(system=_SYSTEM, user=user, schema=_SCHEMA,
                                           tool_description="Emit the seller audit.", max_tokens=1600)
    if narrative is None:
        mode = "heuristic"
        tv = (trust or {}).get("verdict")
        narrative = {
            "summary": f"Heuristic audit of '{s}' (LLM synthesis unavailable). On-chain verdict: {tv or 'unknown'}; "
                       f"{len(endpoints)} advertised endpoint(s); web reputation not synthesized.",
            "reputation": {"level": "unverified" if not trust else ("strong" if tv == "TRUSTED" else "weak" if tv == "AVOID" else "moderate"),
                           "notes": "Derived from on-chain verdict only."},
            "red_flags": ([{"flag": "On-chain AVOID", "severity": "high", "explanation": "Seller-trust returned AVOID."}] if tv == "AVOID" else []),
            "verdict": tv if tv in ("TRUSTED", "CAUTION", "AVOID") else "CAUTION",
            "recommendation": "Re-run with LLM synthesis for a full narrative; verify identity before large commitments.",
            "confidence": 0.4,
        }

    final_verdict = _combine_verdict((trust or {}).get("verdict"), ofac, narrative.get("verdict"))
    reasons = [reason("ONCHAIN_VERDICT", f"On-chain seller-trust: {(trust or {}).get('verdict', 'unavailable')} "
                      f"(score {(trust or {}).get('trust_score', 'n/a')})", 0.3 if (trust or {}).get("verdict") == "AVOID" else -0.2)]
    reasons += [reason(f"RED_FLAG_{i+1}", f"[{f.get('severity')}] {f.get('flag')}", {"low": 0.1, "medium": 0.3, "high": 0.6, "critical": 0.9}.get(f.get("severity"), 0.2))
                for i, f in enumerate(narrative.get("red_flags", [])[:6])]

    receipt = sign_receipt({
        "kind": "x402_seller_audit",
        "seller": s, "resolved_wallet": wallet, "domain": host,
        "verdict": final_verdict, "onchain_verdict": (trust or {}).get("verdict"),
        "onchain_trust_score": (trust or {}).get("trust_score"),
        "endpoints_advertised": len(endpoints), "mode": mode, "as_of": now_iso(),
    })

    shaped = {
        "verdict": final_verdict,
        "confidence": round(float(narrative.get("confidence", 0.5)), 3),
        "query": {"seller": s, "depth": dep, "resolved_wallet": wallet, "domain": host},
        "identity": {"is_wallet_input": is_wallet, "resolved_wallet": wallet, "domain": host,
                     "discovery_available": discovery_ok, "discovery_error": None if discovery_ok else discovery_err},
        "onchain_reputation": {"available": trust is not None, "error": trust_err,
                               "verdict": (trust or {}).get("verdict"), "trust_score": (trust or {}).get("trust_score"),
                               "confidence": (trust or {}).get("confidence"), "metrics": (trust or {}).get("metrics")},
        "endpoints": {"count": len(endpoints), "advertised": endpoints[:40]},
        "liveness": liveness,
        "directory_uptime": directory,
        "reputation": narrative.get("reputation"),
        "red_flags": narrative.get("red_flags", []),
        "summary": narrative.get("summary", ""),
        "recommendation": narrative.get("recommendation", ""),
        "reasons": reasons,
        "web_sources": web_sources,
        "signed_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=False, sources=SOURCES_LABEL,
                                    extra={"mode": mode, "live_web": web["any_ok"], "onchain": trust is not None,
                                           "discovery": discovery_ok, "directory_uptime": directory.get("available")}),
        "error": None if mode == "llm" else {"code": "LLM_FALLBACK", "message": f"Synthesis ran in heuristic mode ({llm_err or 'no key'})."},
        "source": " + ".join(SOURCES_LABEL),
        "timestamp": now_iso(),
        "disclaimer": "Composed x402 seller audit from on-chain settlement data, advertised discovery, and public web "
                      "signals. On-chain reputation uses incoming USDC as a settlement proxy; uptime is not home-probed. "
                      "Heuristic, not legal/financial advice.",
        "cached": False,
    }
    _cache.set(key, shaped)
    return shaped


@router.get("/x402/seller-audit")
async def seller_audit(
    seller: str = Query(..., description="Seller wallet (0x + 40 hex) or domain (e.g. 'api.example.com')."),
    depth: str = Query("shallow", description="On-chain depth: 'shallow' (~200 settlements) or 'deep' (~500)."),
) -> JSONResponse:
    """GET /x402/seller-audit — deep composed audit of an x402 seller: on-chain reputation + advertised endpoints + liveness + web reputation + TRUSTED/CAUTION/AVOID + signed receipt."""
    return JSONResponse(content=await audit(seller, depth))


@router.get("/x402/seller-audit/health")
async def seller_audit_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "seller-audit", "status": "ok",
        "composition": ["/.well-known/x402.json discovery", "/x402/seller-trust", "web signals", "Claude Haiku"],
        "llm_configured": llm_available(), "receipt_signing": receipt_available(),
        "note": "No home probing, no cron. Degrades to heuristic; 502 only if nothing resolves."})
