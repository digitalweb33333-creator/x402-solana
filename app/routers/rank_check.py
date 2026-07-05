"""PREMIUM-3 #2b — Rank-Check ($0.10), the hook that feeds /agent/visibility-audit.

"Where do I rank right now?" — but ONLY the keyword-RELEVANCE rank per category in the
CDP Bazaar discovery/search (what the free explorers x402scan/402index/Agent402 do NOT
expose — they show raw settled-volume rank). One line, high-frequency, near-zero price.

This is deliberately NOT a standalone product: its whole job is to create the daily
reflex and, whenever the rank disappoints, point the agent to /agent/visibility-audit
for the full metadata score + prioritized fixes + signed delta. Reuses the audit's
Bazaar probing and identity resolution (no duplicate logic, no new source/key).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.routers.visibility_audit import (
    _bazaar_configured, _derive_keywords, _fetch_discovery, _harvest, keyword_rank,
)
from app.sources.http_util import client, get_json
from app.verdict import freshness, now_iso

router = APIRouter()

SOURCE = "CDP Bazaar discovery/search (keyword-relevance rank per category)"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
UPSELL = "/agent/visibility-audit"


async def check(seller: str) -> dict:
    s = (seller or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail={"code": "SELLER_REQUIRED", "message": "'seller' (wallet 0x… or origin URL/domain) is required."})
    if not _bazaar_configured():
        raise HTTPException(status_code=502, detail={"code": "BAZAAR_UNAVAILABLE", "message": "Bazaar discovery not reachable on this host; not charged."})

    is_wallet = bool(_ADDR_RE.match(s))
    host, wallet = (None, s) if is_wallet else (None, None)
    if not is_wallet:
        parsed = urlparse(s if "://" in s else f"https://{s}")
        host = (parsed.netloc or parsed.path).strip("/").split("/")[0]
        if not host or "." not in host:
            raise HTTPException(status_code=400, detail={"code": "BAD_SELLER", "message": "'seller' must be a wallet (0x + 40 hex) or a domain/origin URL."})

    endpoints, declared_name = [], None
    if host:
        doc, _ = await _fetch_discovery(host)
        if doc is not None:
            endpoints, pay_to, declared_name = _harvest(doc)
            wallet = wallet or pay_to
    if is_wallet and not endpoints:
        c = await client("audit", timeout=8.0)
        idx, _ = await get_json(c, "https://402index.io/api/search", params={"q": s}, attempts=1)
        if isinstance(idx, dict):
            for it in (idx.get("results") or idx.get("items") or []):
                url = (it.get("resource") or it.get("url") or "") if isinstance(it, dict) else ""
                if url:
                    host = urlparse(url if "://" in url else f"https://{url}").netloc
                    doc, _ = await _fetch_discovery(host)
                    if doc is not None:
                        endpoints, _, declared_name = _harvest(doc)
                    break

    keywords = _derive_keywords(endpoints, declared_name)
    if not keywords:
        raise HTTPException(status_code=502, detail={"code": "NO_CATEGORY",
                            "message": "Could not resolve the seller's category keywords (no reachable discovery doc); not charged."})

    match = host or wallet or s
    ranks = await keyword_rank(keywords, match, want=20, page=20)  # shallow scan (hook = cheap/fast)
    found = [r["rank"] for r in ranks if r["rank"]]
    best = min(found) if found else None
    slipping = [r["keyword"] for r in ranks if not r["rank"]]

    if best is None:
        headline = f"Not in the top 20 for any of your category keywords ({[r['keyword'] for r in ranks]})."
        recommend = True
    elif best > 10:
        headline = f"Best rank #{best} (outside the top 10) — you're being out-ranked."
        recommend = True
    else:
        headline = f"Best rank #{best} across your category keywords."
        recommend = len(slipping) > 0

    as_of = now_iso()
    receipt = sign_receipt({"kind": "agent_rank_check", "seller": s, "host": host, "wallet": wallet,
                            "best_rank": best, "best_keyword_ranks": {r["keyword"]: r["rank"] for r in ranks}, "as_of": as_of})

    return {
        "seller": s, "best_rank": best, "headline": headline,
        "per_keyword": [{"keyword": r["keyword"], "rank": r["rank"], "scanned": r["scanned"]} for r in ranks],
        "category_keywords": keywords,
        "recommendation": (f"Rank is slipping on {slipping or 'your keywords'} → run {UPSELL} for the metadata score, "
                           f"prioritized fixes and a signed delta over time.") if recommend
                          else f"Holding top ranks. Re-check regularly; run {UPSELL} for a full audit + signed snapshot.",
        "upsell": UPSELL,
        "signed_receipt": receipt,
        "note": "Keyword-RELEVANCE rank in CDP Bazaar discovery/search — not the raw settled-volume rank the free explorers show.",
        "data_freshness": freshness(as_of, deterministic=True, sources=[SOURCE]),
        "error": None, "timestamp": as_of, "cached": False,
    }


@router.get("/agent/rank-check")
async def rank_check(
    seller: str = Query(..., description="Seller to check: wallet (0x + 40 hex) or origin URL/domain."),
) -> JSONResponse:
    """GET /agent/rank-check — quick keyword-relevance rank of an x402 seller in Bazaar discovery (the cheap frequent pulse that feeds /agent/visibility-audit)."""
    return JSONResponse(content=await check(seller))


@router.get("/agent/rank-check/health")
async def rank_check_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "rank-check", "status": "ok", "source": SOURCE,
        "bazaar_configured": _bazaar_configured(), "receipt_signing": receipt_available(),
        "note": "Hook for /agent/visibility-audit; keyword-relevance rank only (shallow scan)."})
