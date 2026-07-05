"""PREMIUM-3 #2 — Agent Visibility Audit ($1.00) + Rank-Check hook ($0.10, separate file).

Audits how DISCOVERABLE an x402 agent/seller is across the agent registries and returns
the exact, impact-prioritized fixes — the thing the free explorers (x402scan, 402index,
Agent402) do NOT give: they show raw settled-volume rank; this scores keyword-relevance
rank per category + metadata quality + a top-3 benchmark + a signed delta over time.

What it measures (deterministic):
  1. Metadata quality of the seller's advertised endpoints (from its /.well-known/x402.json):
     dense description, typed input schema, real output.example, tags, llm_usage_prompt.
  2. Keyword-relevance rank per category: query the CDP Bazaar discovery/search with the
     seller's own category keywords and record where it ranks (NOT raw volume rank).
  3. On-chain settle activity (TraceRank proxy) via /x402/seller-trust.
  4. Benchmark vs the top-3 in the seller's category.
  5. Prioritized fixes, ordered by discovery impact.
  6. DELTA vs a previous SIGNED snapshot the client carries back (stateless server).

Output is an Ed25519-signed snapshot the agent stores and re-submits next time to see
"+N places since <date>". The verdict/score/delta are computed by code and signed;
Claude/Haiku only phrases the human-readable summary (never sets a score).

Edge: our own GEO/AEO method pushed this very catalogue to rank #1 — the audit applies
that exact method to any seller. Internal composition + public registries; no new key
(CDP keys already power the facilitator; reused for discovery/search).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.cdp_auth import generate_cdp_jwt
from app.config import CDP_API_KEY_ID, CDP_API_KEY_SECRET, FACILITATOR_URL
from app.llm import compose, llm_available
from app.receipt import sign_receipt, verify_receipt
from app.routers import seller_trust
from app.sources.http_util import TTLCache, client, get_json
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = [
    "Seller /.well-known/x402.json discovery (advertised endpoints + metadata)",
    "CDP Bazaar discovery/search (keyword-relevance rank per category)",
    "402index.io directory (presence, best-effort)",
    "Internal /x402/seller-trust (on-chain settle activity / TraceRank proxy)",
]
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(300)

# Metadata scoring weights (0-100). output.example carries the most discovery weight:
# agents read it to understand the response BEFORE paying (CLAUDE.md discovery block).
_META_WEIGHTS = {
    "output_example": 30,   # a real output example
    "description": 25,      # dense natural-language description (authority + coverage)
    "input_schema": 20,     # typed params with descriptions
    "tags": 15,             # 3-5 category tags
    "llm_usage_prompt": 10, # #1 semantic-discovery lever
}
_META_FIX = {
    "output_example": "Add a REAL output.example to each endpoint (not an empty schema) — agents read it before paying.",
    "description": "Rewrite thin descriptions as one dense sentence: [action] + [precise data] + [authority source] + [coverage]; name the authoritative source.",
    "input_schema": "Give every input param a correct type AND a description with a realistic example; mark required fields.",
    "tags": "Add 3-5 searchable category tags per endpoint (real agent keywords, not internal labels).",
    "llm_usage_prompt": "Add an llm_usage_prompt with the exact agent-facing keywords — the #1 semantic-discovery lever.",
}
_MIN_DESC_LEN = 80


# ---------------------------------------------------------------- Bazaar probing
def _bazaar_configured() -> bool:
    return bool(CDP_API_KEY_ID and CDP_API_KEY_SECRET)


async def bazaar_search(query: str, limit: int, offset: int) -> tuple[list[str] | None, str | None]:
    """Query CDP Bazaar discovery/search; return the ordered list of resource URLs (or error)."""
    if not _bazaar_configured():
        return None, "cdp_keys_missing"
    parts = urlparse(FACILITATOR_URL)
    host, base = parts.netloc, parts.path.rstrip("/")
    try:
        jwt = generate_cdp_jwt(CDP_API_KEY_ID, CDP_API_KEY_SECRET, "GET", host, f"{base}/discovery/search")
    except Exception as exc:
        return None, f"jwt_error_{type(exc).__name__}"
    c = await client("bazaar", timeout=20.0)
    data, err = await get_json(c, f"{FACILITATOR_URL}/discovery/search",
                               params={"query": query, "limit": limit, "offset": offset},
                               headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                               attempts=2)
    if err:
        return None, err
    items = (data or {}).get("resources") or []
    return [it.get("resource", "") for it in items], None


async def keyword_rank(keywords: list[str], match: str, want: int = 40, page: int = 20) -> list[dict]:
    """For each keyword, the seller's best position in Bazaar results. `match` = host or resource substring."""
    out: list[dict] = []
    m = (match or "").lower()
    for kw in keywords:
        seen: list[str] = []
        off, err = 0, None
        while len(seen) < want:
            urls, err = await bazaar_search(kw, page, off)
            if urls is None:
                break
            if not urls:
                break
            if off > 0 and urls and urls[0] in seen:
                break
            seen.extend(urls)
            if len(urls) < page:
                break
            off += page
        rank = next((i for i, u in enumerate(seen[:want], 1) if m and m in (u or "").lower()), None)
        out.append({"keyword": kw, "rank": rank, "scanned": min(len(seen), want),
                    "error": err if urls is None else None,
                    "top1": (seen[0] if seen else None)})
    return out


# ---------------------------------------------------------------- identity + metadata
async def _fetch_discovery(host: str) -> tuple[dict | None, str | None]:
    c = await client("audit", timeout=12.0)
    for path in ("/.well-known/x402.json", "/.well-known/x402"):
        data, err = await get_json(c, f"https://{host}{path}")
        if data is not None:
            return data, None
    return None, err or "no_discovery"


def _harvest(doc: Any) -> tuple[list[dict], str | None, str | None]:
    """Extract advertised endpoints (with metadata) + payTo + declared name from any x402.json."""
    endpoints, pay_to, name = [], None, None
    if isinstance(doc, dict):
        name = doc.get("name")
        pay_to = doc.get("pay_to") or doc.get("payTo")
        res = doc.get("resources")
        if isinstance(res, list):
            for e in res:
                if isinstance(e, dict) and e.get("resource"):
                    endpoints.append(e)
    return endpoints, (pay_to.strip() if isinstance(pay_to, str) else None), name


def _score_endpoint(e: dict) -> dict[str, Any]:
    """Deterministic per-endpoint metadata score + which levers are missing."""
    desc = (e.get("description") or "").strip()
    tags = e.get("tags") or []
    inp = e.get("input") or {}
    qp = inp.get("queryParams") or inp.get("bodyParams") or {}
    out_ex = (e.get("output") or {}).get("example")
    llm = (e.get("llm_usage_prompt") or "").strip()

    present = {
        "description": len(desc) >= _MIN_DESC_LEN,
        "tags": isinstance(tags, list) and len(tags) >= 3,
        "input_schema": isinstance(qp, dict) and len(qp) > 0
                        and all(isinstance(v, dict) and v.get("type") and v.get("description") for v in qp.values()),
        "output_example": bool(out_ex),
        "llm_usage_prompt": bool(llm),
    }
    score = sum(w for k, w in _META_WEIGHTS.items() if present.get(k))
    missing = [k for k in _META_WEIGHTS if not present.get(k)]
    return {"resource": e.get("resource"), "score": score, "present": present, "missing": missing,
            "description_length": len(desc)}


def _derive_keywords(endpoints: list[dict], declared_name: str | None) -> list[str]:
    """Category keywords from the seller's own tags (what it wants to be found for)."""
    tags = Counter()
    for e in endpoints:
        for t in (e.get("tags") or []):
            if isinstance(t, str) and len(t) >= 3:
                tags[t.lower()] += 1
    kws = [t for t, _ in tags.most_common(4)]
    if not kws and declared_name:
        kws = [declared_name.lower()]
    return kws[:4]


def _rank_score(ranks: list[dict]) -> tuple[int, int | None]:
    """Map the best keyword rank to a 0-100 discovery-rank score."""
    found = [r["rank"] for r in ranks if r["rank"]]
    if not found:
        return 0, None
    best = min(found)
    for threshold, pts in ((1, 100), (3, 85), (10, 65), (20, 45), (40, 30)):
        if best <= threshold:
            return pts, best
    return 20, best


# ---------------------------------------------------------------- delta from snapshot
def _delta(snapshot: str | None, overall: int, meta: int, rank_score: int, ranks: list[dict]) -> dict[str, Any]:
    if not snapshot:
        return {"available": False, "reason": "no prior snapshot supplied"}
    import json
    try:
        prev = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    except Exception:
        return {"available": False, "reason": "snapshot not valid JSON"}
    if not (isinstance(prev, dict) and verify_receipt(prev)):
        return {"available": False, "reason": "snapshot signature invalid — ignored (not issued by us or tampered)"}
    claims = prev.get("claims", {})
    if claims.get("kind") != "agent_visibility_snapshot":
        return {"available": False, "reason": "snapshot is not an agent_visibility_snapshot"}
    prev_ranks = claims.get("best_keyword_ranks", {}) or {}
    per_kw = []
    for r in ranks:
        pr = prev_ranks.get(r["keyword"])
        cur = r["rank"]
        if isinstance(pr, int) and isinstance(cur, int):
            per_kw.append({"keyword": r["keyword"], "previous_rank": pr, "current_rank": cur, "places_gained": pr - cur})
    return {"available": True, "since": claims.get("as_of"),
            "overall_delta": overall - int(claims.get("overall_score", overall)),
            "metadata_delta": meta - int(claims.get("metadata_score", meta)),
            "rank_score_delta": rank_score - int(claims.get("rank_score", rank_score)),
            "per_keyword": per_kw}


# ---------------------------------------------------------------- narrative (presentation only)
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "description": "3-4 sentence plain-language read of the seller's discoverability and the single highest-impact fix."}},
    "required": ["summary"], "additionalProperties": False,
}


async def _summary(facts: dict) -> tuple[str, str]:
    if not llm_available():
        return (f"Visibility score {facts['overall_score']}/100 (metadata {facts['metadata_score']}/100, "
                f"rank {facts['rank_score']}/100, settle {facts['settle_score']}/100). "
                f"Top fix: {facts['top_fix']}"), "heuristic"
    sys = ("You explain an x402 seller's DISCOVERABILITY audit to the agent that operates it. Use ONLY the "
           "provided numbers and fixes. Do not invent scores. Be concrete and prioritized; name the single "
           "highest-impact fix. 3-4 sentences.")
    user = (f"OVERALL {facts['overall_score']}/100 | metadata {facts['metadata_score']}/100 | rank {facts['rank_score']}/100 "
            f"| settle {facts['settle_score']}/100.\nBEST KEYWORD RANKS: {facts['best_ranks']}\n"
            f"PRIORITIZED FIXES: {facts['fixes']}\nENDPOINTS SCORED: {facts['endpoints_scored']}")
    out, err = await compose(system=sys, user=user, schema=_SUMMARY_SCHEMA, tool_description="Emit the summary.", max_tokens=400)
    if out and out.get("summary"):
        return out["summary"], "llm"
    return (f"Visibility score {facts['overall_score']}/100. Top fix: {facts['top_fix']}"), f"heuristic({err})"


# ---------------------------------------------------------------- main
async def audit(seller: str, snapshot: str | None) -> dict[str, Any]:
    s = (seller or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail={"code": "SELLER_REQUIRED", "message": "'seller' (wallet 0x… or origin URL/domain) is required."})

    is_wallet = bool(_ADDR_RE.match(s))
    host, wallet = (None, s) if is_wallet else (None, None)
    if not is_wallet:
        parsed = urlparse(s if "://" in s else f"https://{s}")
        host = (parsed.netloc or parsed.path).strip("/").split("/")[0]
        if not host or "." not in host:
            raise HTTPException(status_code=400, detail={"code": "BAD_SELLER", "message": "'seller' must be a wallet (0x + 40 hex) or a domain/origin URL."})

    # --- resolve discovery doc + endpoints ---
    endpoints, pay_to, declared_name, discovery_err = [], None, None, None
    if host:
        doc, discovery_err = await _fetch_discovery(host)
        if doc is not None:
            endpoints, pay_to, declared_name = _harvest(doc)
    wallet = wallet or pay_to
    # wallet-only input: try to locate its origin via 402index, then fetch discovery
    if is_wallet and not endpoints:
        c = await client("audit", timeout=8.0)
        idx, _ = await get_json(c, "https://402index.io/api/search", params={"q": s}, attempts=1)
        cand = None
        if isinstance(idx, dict):
            for it in (idx.get("results") or idx.get("items") or []):
                url = (it.get("resource") or it.get("url") or "") if isinstance(it, dict) else ""
                if url:
                    cand = urlparse(url if "://" in url else f"https://{url}").netloc
                    break
        if cand:
            host = cand
            doc, discovery_err = await _fetch_discovery(host)
            if doc is not None:
                endpoints, pay_to, declared_name = _harvest(doc)

    # --- on-chain settle activity (TraceRank proxy) ---
    settle = {"available": False}
    if wallet and _ADDR_RE.match(wallet):
        try:
            t = await seller_trust.assess(wallet, "shallow")
            m = t.get("metrics") or {}
            settle = {"available": True, "verdict": t.get("verdict"), "trust_score": t.get("trust_score"),
                      "settlement_count": m.get("settlement_count"), "unique_counterparties": m.get("unique_counterparties"),
                      "wallet_age_days": m.get("wallet_age_days")}
        except HTTPException as exc:
            settle = {"available": False, "reason": (exc.detail or {}).get("code") if isinstance(exc.detail, dict) else f"http_{exc.status_code}"}
        except Exception as exc:
            settle = {"available": False, "reason": type(exc).__name__}

    if not endpoints and not settle.get("available"):
        raise HTTPException(status_code=502, detail={"code": "NOTHING_RESOLVED",
                            "message": "Could not resolve the seller's discovery doc or any on-chain activity; not charged."})

    # --- metadata scoring (deterministic) ---
    scored = [_score_endpoint(e) for e in endpoints]
    metadata_score = round(sum(x["score"] for x in scored) / len(scored)) if scored else 0

    # --- keyword-relevance rank per category ---
    keywords = _derive_keywords(endpoints, declared_name)
    match = host or wallet or s
    ranks = await keyword_rank(keywords, match, want=40, page=20) if keywords else []
    rank_score, best_rank = _rank_score(ranks)

    # --- benchmark vs top-3 in the primary category ---
    benchmark = {"available": False}
    if ranks and ranks[0].get("top1"):
        top_urls, berr = await bazaar_search(keywords[0], 3, 0)
        benchmark = {"available": bool(top_urls), "category": keywords[0],
                     "top3": (top_urls or [])[:3], "reason": berr,
                     "note": "Compare your metadata completeness to these top-3 for your primary category keyword."}

    # --- settle score (TraceRank proxy) ---
    if settle.get("available"):
        sc = settle.get("settlement_count") or 0
        settle_score = int(clamp01((settle.get("trust_score") or 0) / 100.0 * 0.6 + min(1.0, sc / 20.0) * 0.4) * 100)
    else:
        settle_score = 0

    overall = round(0.5 * metadata_score + 0.3 * rank_score + 0.2 * settle_score)

    # --- prioritized fixes (deterministic, by aggregate impact) ---
    miss_impact: Counter = Counter()
    for x in scored:
        for k in x["missing"]:
            miss_impact[k] += _META_WEIGHTS[k]
    fixes = [{"issue": k, "endpoints_affected": sum(1 for x in scored if k in x["missing"]),
              "impact": miss_impact[k], "fix": _META_FIX[k]}
             for k in sorted(miss_impact, key=lambda k: -miss_impact[k])]
    if rank_score < 65:
        fixes.append({"issue": "low_keyword_rank",
                      "endpoints_affected": len(scored),
                      "impact": 40,
                      "fix": f"Not in the top ranks for your category keywords ({[r['keyword'] for r in ranks]}). "
                             "Enrich descriptions with the exact phrases agents type, and earn real 3rd-party settles."})
    if settle.get("available") and (settle.get("settlement_count") or 0) < 3:
        fixes.append({"issue": "thin_settle_activity", "endpoints_affected": len(scored) or 1, "impact": 30,
                      "fix": "Discovery ranking is usage-driven (TraceRank): earn real 3rd-party payers, not self-settles."})
    fixes.sort(key=lambda f: -f["impact"])
    top_fix = fixes[0]["fix"] if fixes else "Metadata is complete; focus on earning real 3rd-party settles to climb the ranking."

    reasons = []
    if scored:
        reasons.append(reason("METADATA_SCORE", f"Average endpoint metadata completeness {metadata_score}/100 across {len(scored)} endpoints.", (metadata_score - 60) / -100.0))
    reasons.append(reason("RANK_SCORE", f"Best category keyword rank: {('#'+str(best_rank)) if best_rank else 'not in top 40'}.", (rank_score - 60) / -100.0))
    if settle.get("available"):
        reasons.append(reason("SETTLE_ACTIVITY", f"On-chain settle activity: {settle.get('settlement_count')} settlement(s), trust {settle.get('trust_score')}.", (settle_score - 60) / -100.0))

    best_ranks = {r["keyword"]: r["rank"] for r in ranks}
    delta = _delta(snapshot, overall, metadata_score, rank_score, ranks)

    facts = {"overall_score": overall, "metadata_score": metadata_score, "rank_score": rank_score,
             "settle_score": settle_score, "best_ranks": best_ranks, "fixes": [f["fix"] for f in fixes[:5]],
             "endpoints_scored": len(scored), "top_fix": top_fix}
    summary, mode = await _summary(facts)

    as_of = now_iso()
    snapshot_receipt = sign_receipt({
        "kind": "agent_visibility_snapshot",
        "seller": s, "host": host, "wallet": wallet,
        "overall_score": overall, "metadata_score": metadata_score,
        "rank_score": rank_score, "settle_score": settle_score,
        "best_keyword_ranks": best_ranks, "endpoints_scored": len(scored),
        "as_of": as_of,
    })

    return {
        "overall_score": overall,
        "scores": {"metadata": metadata_score, "keyword_rank": rank_score, "settle_activity": settle_score,
                   "weights": {"metadata": 0.5, "keyword_rank": 0.3, "settle_activity": 0.2}},
        "summary": summary,
        "query": {"seller": s, "resolved_host": host, "resolved_wallet": wallet, "is_wallet_input": is_wallet},
        "identity": {"declared_name": declared_name, "discovery_available": bool(endpoints),
                     "discovery_error": None if endpoints else discovery_err, "endpoints_scored": len(scored)},
        "metadata_audit": {"average_score": metadata_score, "per_endpoint": scored[:50]},
        "keyword_rank": {"category_keywords": keywords, "best_rank": best_rank, "per_keyword": ranks,
                         "note": "Keyword-RELEVANCE rank in CDP Bazaar discovery/search (not raw settled-volume rank).",
                         "bazaar_available": _bazaar_configured() and any(r.get("error") is None for r in ranks)},
        "benchmark": benchmark,
        "settle_activity": settle,
        "prioritized_fixes": fixes,
        "delta": delta,
        "reasons": reasons,
        "signed_snapshot": snapshot_receipt,
        "snapshot_usage": "Store `signed_snapshot` and pass it back as the `snapshot` param next time to get a dated delta ('+N places since <date>').",
        "data_freshness": freshness(as_of, deterministic=(mode != "llm"), sources=SOURCES,
                                    extra={"summary_mode": mode, "bazaar_configured": _bazaar_configured()}),
        "error": None,
        "source": " + ".join(SOURCES),
        "timestamp": as_of,
        "disclaimer": "Discoverability audit across x402 agent registries. Scores/deltas are computed deterministically and "
                      "signed; the summary is a plain-language explanation only. Ranking is usage-driven — metadata is the "
                      "controllable lever, real 3rd-party settles are the rest. Not a guarantee of placement.",
        "cached": False,
    }


@router.get("/agent/visibility-audit")
async def visibility_audit(
    seller: str = Query(..., description="Seller to audit: wallet (0x + 40 hex) or origin URL/domain, e.g. 'api.example.com'."),
    snapshot: str | None = Query(None, description="Optional: the signed_snapshot JSON from a previous audit, to compute a dated delta."),
) -> JSONResponse:
    """GET /agent/visibility-audit — discoverability audit of an x402 seller across the agent registries: metadata score + keyword-relevance rank + top-3 benchmark + prioritized fixes + signed delta snapshot."""
    return JSONResponse(content=await audit(seller, snapshot))


@router.get("/agent/visibility-audit/health")
async def visibility_audit_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "visibility-audit", "status": "ok",
        "composition": ["/.well-known/x402.json discovery", "CDP Bazaar discovery/search", "402index.io", "/x402/seller-trust", "Claude Haiku (summary only)"],
        "bazaar_configured": _bazaar_configured(), "llm_configured": llm_available(), "receipt_signing": receipt_available(),
        "note": "Scores/deltas deterministic + signed; LLM only phrases the summary. Stateless: client carries the signed snapshot for deltas."})
