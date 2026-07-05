"""LOT 8 #7 — Wallet Forensics ($0.15), multi-hop extension of wallet-xray.

Traverses a wallet's ERC-20 counterparty graph across 1-3 hops and returns the flow
graph (nodes + main edges), ranked counterparties and detected patterns (flow
concentration, hubs, linked/shared counterparties, circular flows) + a signed receipt.

Differentiator vs /crypto/wallet-xray ($0.05, single-hop balances/valuation; cf
RAPPORT-BENCHMARK-12): this is the MULTI-HOP flow graph + pattern detection — the
deliverable must show the multi-hop, otherwise it is just a pricier x-ray.

Compute is bounded: fan-out per hop is capped (top-N counterparties), pages per node
shrink with depth, and each Blockscout call has a short timeout. A wallet-xray header
(balances + OFAC flag) is folded in best-effort. Source: Base/EVM Blockscout (keyless).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.routers import wallet_xray
from app.sources.base_chain import BLOCKSCOUT_HOSTS, erc20_edges
from app.sources.http_util import TTLCache
from app.verdict import freshness, now_iso, reason

router = APIRouter()

SOURCE = "EVM Blockscout ERC-20 transfer graph (multi-hop) + OFAC SDN list"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(300)

# Fan-out / page caps per hop (protect latency + rate limit).
_HOP1_PAGES = 4
_HOP1_TOPN = 15          # counterparties surfaced at hop 1
_EXPAND_K = {2: 6, 3: 3}  # how many nodes to expand into the next hop, per requested depth level
_EXPAND_PAGES = 2


def _degree(edge: dict) -> int:
    return edge["in_count"] + edge["out_count"]


async def _edges_for(addr: str, host: str, pages: int) -> dict[str, dict]:
    data, _ = await erc20_edges(addr, host, max_pages=pages)
    return data or {}


def _detect_patterns(target: str, hop1: dict[str, dict], hop2_map: dict[str, dict[str, dict]]) -> list[dict]:
    patterns: list[dict] = []
    total_deg = sum(_degree(e) for e in hop1.values()) or 1

    # 1) Flow concentration — largest counterparty share of edge volume.
    if hop1:
        top_cp, top_edge = max(hop1.items(), key=lambda kv: _degree(kv[1]))
        share = _degree(top_edge) / total_deg
        if share >= 0.5:
            patterns.append({"pattern": "FLOW_CONCENTRATION", "severity": "medium",
                             "detail": f"Top counterparty accounts for {share*100:.0f}% of transfer flow ({top_cp}).",
                             "weight": 0.4})

    # 2) Circular flow — a hop-2 path returns to the target.
    circular = [mid for mid, sub in hop2_map.items() if target in sub]
    if circular:
        patterns.append({"pattern": "CIRCULAR_FLOW", "severity": "high",
                         "detail": f"Funds cycle back to the origin through {len(circular)} intermediary wallet(s).",
                         "weight": 0.6})

    # 3) Linked wallets — a hop-2 node shared by several hop-1 nodes (common hub between them).
    shared: dict[str, int] = {}
    for sub in hop2_map.values():
        for node in sub:
            if node != target and node not in hop1:
                shared[node] = shared.get(node, 0) + 1
    linked = sorted([n for n, c in shared.items() if c >= 2], key=lambda n: -shared[n])[:5]
    if linked:
        patterns.append({"pattern": "LINKED_WALLETS", "severity": "medium",
                         "detail": f"{len(linked)} wallet(s) are shared counterparties of multiple direct counterparties (possible cluster).",
                         "weight": 0.3, "examples": linked})

    # 4) Hub — a direct counterparty with very high degree (mixer/exchange/deployer signature).
    hubs = [cp for cp, e in hop1.items() if _degree(e) >= 40]
    if hubs:
        patterns.append({"pattern": "HIGH_DEGREE_HUB", "severity": "info",
                         "detail": f"{len(hubs)} counterparty(ies) show hub-like activity (exchange/mixer/deployer or high-traffic contract).",
                         "weight": 0.1, "examples": hubs[:5]})
    return patterns


async def analyze(wallet: str, depth: int, chain: str) -> dict[str, Any]:
    addr = (wallet or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail={"code": "BAD_WALLET", "message": "'wallet' must be an EVM address (0x + 40 hex)."})
    if depth not in (1, 2, 3):
        raise HTTPException(status_code=400, detail={"code": "BAD_DEPTH", "message": "'depth' must be 1, 2 or 3."})
    ch = (chain or "base").strip().lower()
    host = BLOCKSCOUT_HOSTS.get(ch)
    if not host:
        raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_CHAIN", "message": f"'chain' must be one of: {', '.join(sorted(BLOCKSCOUT_HOSTS))}."})

    key = f"{ch}|{addr.lower()}|{depth}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    low = addr.lower()
    hop1 = await _edges_for(low, host, _HOP1_PAGES)
    if not hop1:
        raise HTTPException(status_code=502, detail={"code": "NO_GRAPH", "message": "No ERC-20 transfer graph found for this wallet on this chain (unknown wallet or source unavailable); not charged."})

    # Rank hop-1 counterparties by degree.
    ranked = sorted(hop1.items(), key=lambda kv: _degree(kv[1]), reverse=True)
    top_nodes = [cp for cp, _ in ranked[:_HOP1_TOPN]]

    # Expand into further hops (bounded fan-out).
    hop2_map: dict[str, dict[str, dict]] = {}
    max_hop = depth
    if max_hop >= 2:
        expand = [cp for cp, _ in ranked[:_EXPAND_K[2]]]
        subs = await asyncio.gather(*[_edges_for(cp, host, _EXPAND_PAGES) for cp in expand])
        hop2_map = {cp: sub for cp, sub in zip(expand, subs)}
    if max_hop >= 3:
        # expand the most active hop-2 nodes one more level
        hop2_nodes = sorted({n: e for sub in hop2_map.values() for n, e in sub.items() if n != low}.items(),
                            key=lambda kv: _degree(kv[1]), reverse=True)
        expand3 = [n for n, _ in hop2_nodes[:_EXPAND_K[3]]]
        subs3 = await asyncio.gather(*[_edges_for(n, host, 1) for n in expand3])
        for n, sub in zip(expand3, subs3):
            hop2_map.setdefault(n, sub)

    patterns = _detect_patterns(low, hop1, hop2_map)

    # Optional wallet-xray header (balances + OFAC flag), best-effort.
    header = {"available": False}
    try:
        x = await wallet_xray.xray(addr, ch)
        header = {"available": True,
                  "portfolio_value_usd": (x.get("portfolio") or {}).get("total_value_usd"),
                  "token_count": (x.get("portfolio") or {}).get("token_count"),
                  "ofac_listed": (x.get("sanction_check") or {}).get("ofac_listed"),
                  "risk_flags": x.get("risk_flags") or []}
    except Exception as exc:
        header = {"available": False, "reason": type(exc).__name__}

    counterparties = [{
        "address": cp, "direction": ("bidirectional" if e["in_count"] and e["out_count"]
                                     else ("in" if e["in_count"] else "out")),
        "in_count": e["in_count"], "out_count": e["out_count"], "degree": _degree(e),
        "tokens": sorted(e["tokens"])[:6], "last_activity": e["last_ts"],
    } for cp, e in ranked[:_HOP1_TOPN]]

    # Build a compact edge list for the graph (hop1 + expanded hop2 edges).
    edges_out = [{"from": low, "to": cp, "hop": 1, "degree": _degree(e)} for cp, e in ranked[:_HOP1_TOPN]]
    for mid, sub in hop2_map.items():
        for n, e in sorted(sub.items(), key=lambda kv: _degree(kv[1]), reverse=True)[:5]:
            if n != low:
                edges_out.append({"from": mid, "to": n, "hop": 2, "degree": _degree(e)})

    reasons = [reason(p["pattern"], p["detail"], p.get("weight", 0.2)) for p in patterns] or \
              [reason("NO_STRONG_PATTERN", "No strong structural pattern detected in the sampled graph.", -0.2)]

    receipt = sign_receipt({
        "kind": "wallet_forensics",
        "wallet": addr, "chain": ch, "depth": depth,
        "direct_counterparties": len(hop1),
        "patterns": [p["pattern"] for p in patterns],
        "as_of": now_iso(),
    })

    shaped = {
        "query": {"wallet": addr, "depth": depth, "chain": ch},
        "header": header,
        "graph_summary": {
            "hops_traversed": max_hop,
            "direct_counterparties": len(hop1),
            "nodes_sampled": 1 + len(top_nodes) + sum(len(s) for s in hop2_map.values()),
            "edges_sampled": len(edges_out),
            "total_direct_transfers": sum(_degree(e) for e in hop1.values()),
        },
        "top_counterparties": counterparties,
        "graph_edges": edges_out[:60],
        "patterns": patterns,
        "reasons": reasons,
        "signed_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=True, sources=[SOURCE],
                                    extra={"chain": ch, "depth": depth, "fan_out_capped": True}),
        "source": SOURCE,
        "timestamp": now_iso(),
        "disclaimer": "Best-effort flow forensics from a sampled ERC-20 transfer graph (fan-out and history are capped). "
                      "Counterparty roles are heuristic; a hub may be a benign exchange/contract. Not legal/financial advice.",
        "cached": False,
    }
    _cache.set(key, shaped)
    return shaped


@router.get("/crypto/wallet-forensics")
async def wallet_forensics(
    wallet: str = Query(..., description="EVM wallet address (0x + 40 hex) to investigate."),
    depth: int = Query(2, description="Graph hops to traverse: 1, 2 or 3 (default 2)."),
    chain: str = Query("base", description="base | ethereum | optimism | polygon | arbitrum | gnosis (default base)"),
) -> JSONResponse:
    """GET /crypto/wallet-forensics — multi-hop counterparty flow graph + pattern detection (concentration, circular, linked, hubs) + signed receipt."""
    return JSONResponse(content=await analyze(wallet, depth, chain))


@router.get("/crypto/wallet-forensics/health")
async def wallet_forensics_health() -> JSONResponse:
    from app.receipt import receipt_available
    from app.sources.http_util import client, get_json
    c = await client("blockscout")
    _, err = await get_json(c, "https://base.blockscout.com/api/v2/stats")
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "wallet-forensics", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "receipt_signing": receipt_available(), "cache_entries": len(_cache)})
