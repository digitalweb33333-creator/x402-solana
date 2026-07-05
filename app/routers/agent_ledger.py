"""LOT 8 #2 — Agent Ledger ($0.04).

"Your agent's books in one call." Structured, ready-to-account report of a wallet's
USDC activity on Base (the settlement asset for x402): revenue (incoming) vs expenses
(outgoing), transaction count, net, top counterparties and a period breakdown.

Niche is empty (cf RAPPORT-BENCHMARK-12): the raw ledger is public on Basescan, but
nobody packages it as a payable, structured accounting report. Value = the STRUCTURING
(revenue/expense classification, netting, counterparty ranking, period buckets), not
data access.

Source: Base Blockscout (keyless) via `base_chain.usdc_transfers_ledger` (both directions).
TTL 5 min. No new key. Deterministic aggregation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.base_chain import usdc_transfers_ledger
from app.sources.http_util import TTLCache, utc_now
from app.verdict import age_seconds, freshness, now_iso

router = APIRouter()

SOURCE = "Base Blockscout USDC transfers (base.blockscout.com), settlement asset for x402"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
_cache = TTLCache(300)


def _parse_ts(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


def _rank_counterparties(rows: list[dict], direction: str, top: int = 5) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"volume_usdc": 0.0, "tx_count": 0})
    for r in rows:
        if r["direction"] != direction or not r.get("counterparty"):
            continue
        a = agg[r["counterparty"]]
        a["volume_usdc"] += r.get("value_usdc") or 0.0
        a["tx_count"] += 1
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["volume_usdc"], reverse=True)[:top]
    return [{"address": addr, "volume_usdc": _round2(v["volume_usdc"]), "tx_count": v["tx_count"]} for addr, v in ranked]


def _weekly_breakdown(rows: list[dict]) -> list[dict]:
    """Aggregate per ISO week (revenue/expense/net), most recent first, max 13 buckets."""
    buckets: dict[str, dict] = defaultdict(lambda: {"revenue_usdc": 0.0, "expenses_usdc": 0.0, "tx_count": 0})
    for r in rows:
        dt = _parse_ts(r.get("ts"))
        if dt is None:
            continue
        iso = dt.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        b = buckets[key]
        val = r.get("value_usdc") or 0.0
        if r["direction"] == "in":
            b["revenue_usdc"] += val
        else:
            b["expenses_usdc"] += val
        b["tx_count"] += 1
    out = []
    for key in sorted(buckets, reverse=True)[:13]:
        b = buckets[key]
        out.append({"week": key, "revenue_usdc": _round2(b["revenue_usdc"]),
                    "expenses_usdc": _round2(b["expenses_usdc"]),
                    "net_usdc": _round2(b["revenue_usdc"] - b["expenses_usdc"]), "tx_count": b["tx_count"]})
    return out


async def build_ledger(wallet: str, period: str) -> dict[str, Any]:
    addr = (wallet or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail={"code": "BAD_WALLET", "message": "'wallet' must be an EVM address (0x + 40 hex)."})
    per = (period or "30d").strip().lower()
    if per not in _PERIODS:
        raise HTTPException(status_code=400, detail={"code": "BAD_PERIOD", "message": "'period' must be one of: 7d, 30d, 90d, all."})

    key = f"{addr.lower()}|{per}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    rows, err = await usdc_transfers_ledger(addr, max_pages=10)
    if rows is None:
        raise HTTPException(status_code=502, detail={"code": "SOURCE_UNAVAILABLE", "message": f"Base Blockscout unreachable ({err}); not charged."})

    # Period filter
    days = _PERIODS[per]
    if days is not None and rows:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = [r for r in rows if (_parse_ts(r.get("ts")) or datetime.now(timezone.utc)) >= cutoff]

    revenue = sum(r["value_usdc"] for r in rows if r["direction"] == "in" and r.get("value_usdc"))
    expenses = sum(r["value_usdc"] for r in rows if r["direction"] == "out" and r.get("value_usdc"))
    in_rows = [r for r in rows if r["direction"] == "in"]
    out_rows = [r for r in rows if r["direction"] == "out"]
    all_ts = [t for t in (_parse_ts(r.get("ts")) for r in rows) if t is not None]
    first = min(all_ts).strftime("%Y-%m-%dT%H:%M:%SZ") if all_ts else None
    last = max(all_ts).strftime("%Y-%m-%dT%H:%M:%SZ") if all_ts else None
    largest_rev = max((r["value_usdc"] for r in in_rows if r.get("value_usdc")), default=0.0)
    largest_exp = max((r["value_usdc"] for r in out_rows if r.get("value_usdc")), default=0.0)

    shaped = {
        "query": {"wallet": addr, "period": per},
        "summary": {
            "revenue_usdc": _round2(revenue),
            "expenses_usdc": _round2(expenses),
            "net_usdc": _round2(revenue - expenses),
            "currency": "USDC",
            "tx_count": len(rows),
            "revenue_tx_count": len(in_rows),
            "expense_tx_count": len(out_rows),
            "unique_payers": len({r["counterparty"] for r in in_rows if r.get("counterparty")}),
            "unique_payees": len({r["counterparty"] for r in out_rows if r.get("counterparty")}),
            "avg_revenue_usdc": _round2(revenue / len(in_rows)) if in_rows else 0.0,
            "avg_expense_usdc": _round2(expenses / len(out_rows)) if out_rows else 0.0,
            "largest_revenue_usdc": _round2(largest_rev),
            "largest_expense_usdc": _round2(largest_exp),
            "first_activity": first,
            "last_activity": last,
        },
        "top_payers": _rank_counterparties(rows, "in"),
        "top_payees": _rank_counterparties(rows, "out"),
        "weekly_breakdown": _weekly_breakdown(rows),
        "notes": [
            "Revenue = incoming USDC (x402 settlements received); expenses = outgoing USDC.",
            "Ledger reflects USDC transfers on Base only; not every USDC transfer is an x402 payment.",
            "Coverage is best-effort from the indexer's most recent pages (large histories may be truncated).",
        ],
        "data_freshness": freshness(last, deterministic=True, sources=[SOURCE],
                                    extra={"period_days": days, "row_count": len(rows),
                                           "last_activity_age_seconds": age_seconds(last)}),
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Automated bookkeeping of on-chain USDC activity on Base; informational, not accounting, tax or financial advice. Reconcile against your own records.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/agent/ledger")
async def agent_ledger(
    wallet: str = Query(..., description="EVM wallet address (0x + 40 hex) to report on."),
    period: str = Query("30d", description="Reporting window: 7d | 30d | 90d | all (default 30d)."),
) -> JSONResponse:
    """GET /agent/ledger — structured USDC accounting for a wallet: revenue vs expenses, net, top counterparties, weekly breakdown."""
    return JSONResponse(content=await build_ledger(wallet, period))


@router.get("/agent/ledger/health")
async def agent_ledger_health() -> JSONResponse:
    from app.sources.http_util import client, get_json
    c = await client("blockscout")
    data, err = await get_json(c, "https://base.blockscout.com/api/v2/stats")
    ok = err is None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "agent-ledger", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
