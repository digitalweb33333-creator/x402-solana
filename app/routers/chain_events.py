"""LOT 8 #8 — On-chain Events ($0.05).

On-demand, DECODED on-chain events for AI agents: wraps eth_getLogs on Base and
normalizes the most common events (Transfer, Approval, ERC-1155 TransferSingle/Batch,
Uniswap V2/V3 Swap, Sync) into readable fields — raw topics/data when the signature is
unknown. Value-add = decoding + normalization + no RPC setup (cf RAPPORT-BENCHMARK-12).

RPC: ALCHEMY_BASE_URL (preferred). If unset/failing, falls back to the public Base RPC
and flags `rpc.fallback_used=true` in the response (per spec — signal the fallback, no mock).

Block span and result count are capped to protect the free-tier rate limit. Sources down
→ 502/504 (agent not charged). Arbitrary events supported via `event` = a signature string
(topic0 computed with keccac) or a raw 0x topic0.
"""
from __future__ import annotations

import re
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import ALCHEMY_BASE_URL
from app.sources.http_util import TTLCache, client, post_json, utc_now

router = APIRouter()

PUBLIC_BASE_RPC = "https://mainnet.base.org"
SOURCE = "Base JSON-RPC eth_getLogs (Alchemy, fallback public RPC)"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_TOPIC_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
_SIG_RE = re.compile(r"^[A-Za-z_]\w*\([^)]*\)$")

_MAX_SPAN = 5000        # max block range (Base ~2s/block ≈ 2.7h)
_DEFAULT_LOOKBACK = 2000
_MAX_LIMIT = 100
_cache = TTLCache(30)   # short: fresh logs

# Known event decoders keyed by topic0.
# Each: {name, signature, indexed:[(name,type)], data_types:[...], data_names:[...]}.
_KNOWN: dict[str, dict] = {}


def _sig(signature: str, indexed: list[tuple[str, str]], data_types: list[str], data_names: list[str], label: str) -> None:
    topic0 = "0x" + keccak(text=signature).hex()
    _KNOWN[topic0] = {"name": label, "signature": signature, "indexed": indexed,
                      "data_types": data_types, "data_names": data_names}


_sig("Transfer(address,address,uint256)", [("from", "address"), ("to", "address")], ["uint256"], ["value"], "Transfer")
_sig("Approval(address,address,uint256)", [("owner", "address"), ("spender", "address")], ["uint256"], ["value"], "Approval")
_sig("TransferSingle(address,address,address,uint256,uint256)", [("operator", "address"), ("from", "address"), ("to", "address")], ["uint256", "uint256"], ["id", "value"], "TransferSingle")
_sig("TransferBatch(address,address,address,uint256[],uint256[])", [("operator", "address"), ("from", "address"), ("to", "address")], ["uint256[]", "uint256[]"], ["ids", "values"], "TransferBatch")
_sig("Swap(address,uint256,uint256,uint256,uint256,address)", [("sender", "address"), ("to", "address")], ["uint256", "uint256", "uint256", "uint256"], ["amount0In", "amount1In", "amount0Out", "amount1Out"], "Swap(V2)")
_sig("Swap(address,address,int256,int256,uint160,uint128,int24)", [("sender", "address"), ("recipient", "address")], ["int256", "int256", "uint160", "uint128", "int24"], ["amount0", "amount1", "sqrtPriceX96", "liquidity", "tick"], "Swap(V3)")
_sig("Sync(uint112,uint112)", [], ["uint112", "uint112"], ["reserve0", "reserve1"], "Sync")
_sig("Mint(address,uint256,uint256)", [("sender", "address")], ["uint256", "uint256"], ["amount0", "amount1"], "Mint")
_sig("Burn(address,uint256,uint256,address)", [("sender", "address"), ("to", "address")], ["uint256", "uint256"], ["amount0", "amount1"], "Burn")

# Friendly names → representative topic0 filter (first matching signature).
_NAME_TO_TOPIC = {
    "transfer": "0x" + keccak(text="Transfer(address,address,uint256)").hex(),
    "approval": "0x" + keccak(text="Approval(address,address,uint256)").hex(),
    "swap": "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex(),
    "sync": "0x" + keccak(text="Sync(uint112,uint112)").hex(),
    "mint": "0x" + keccak(text="Mint(address,uint256,uint256)").hex(),
    "burn": "0x" + keccak(text="Burn(address,uint256,uint256,address)").hex(),
    "transfersingle": "0x" + keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex(),
}


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:]


def _resolve_topic0(event: str | None) -> str | None:
    if not event or not event.strip():
        return None
    e = event.strip()
    if _TOPIC_RE.match(e):
        return e.lower()
    if e.lower() in _NAME_TO_TOPIC:
        return _NAME_TO_TOPIC[e.lower()]
    if _SIG_RE.match(e):
        return "0x" + keccak(text=e).hex()
    raise HTTPException(status_code=400, detail={"code": "BAD_EVENT",
                        "message": "'event' must be a known name (Transfer, Approval, Swap, Mint, Burn, TransferSingle, Sync), a full signature like 'Transfer(address,address,uint256)', or a 0x-prefixed 32-byte topic0."})


def _decode_log(log: dict) -> dict[str, Any]:
    topics = log.get("topics") or []
    topic0 = (topics[0].lower() if topics else None)
    base = {
        "address": log.get("address"),
        "block_number": int(log["blockNumber"], 16) if log.get("blockNumber") else None,
        "tx_hash": log.get("transactionHash"),
        "log_index": int(log["logIndex"], 16) if log.get("logIndex") else None,
        "topic0": topic0,
    }
    spec = _KNOWN.get(topic0) if topic0 else None
    if not spec:
        return {**base, "event": None, "decoded": False, "topics": topics, "data": log.get("data")}
    out = {"event": spec["name"], "signature": spec["signature"], "decoded": True, "params": {}}
    try:
        # indexed params from topics[1:], named per spec (addresses truncated from 32 bytes)
        for i, (pname, typ) in enumerate(spec["indexed"]):
            t = topics[i + 1] if i + 1 < len(topics) else None
            if t is None:
                continue
            out["params"][pname] = _addr_from_topic(t) if typ == "address" else int(t, 16)
        # non-indexed data
        data_hex = (log.get("data") or "0x")[2:]
        if spec["data_types"] and data_hex:
            values = abi_decode(spec["data_types"], bytes.fromhex(data_hex))
            for name, val in zip(spec["data_names"], values):
                out["params"][name] = val if isinstance(val, (int, str, list)) else str(val)
    except Exception as exc:  # decode failure → keep raw, never fail the call
        return {**base, "event": spec["name"], "decoded": False, "decode_error": type(exc).__name__,
                "topics": topics, "data": log.get("data")}
    return {**base, **out}


async def _rpc(url: str, method: str, params: list) -> tuple[Any, str | None]:
    c = await client("base_rpc", timeout=20.0)
    data, err = await post_json(c, url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if err:
        return None, err
    if isinstance(data, dict) and data.get("error"):
        return None, str(data["error"].get("message", data["error"]))[:200]
    return (data or {}).get("result"), None


async def fetch_events(contract: str, event: str | None, from_block: int | None,
                       to_block: int | None, lookback: int, limit: int) -> dict[str, Any]:
    addr = (contract or "").strip()
    if not _ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail={"code": "BAD_CONTRACT", "message": "'contract' must be an EVM address (0x + 40 hex)."})
    topic0 = _resolve_topic0(event)
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(status_code=400, detail={"code": "BAD_LIMIT", "message": f"'limit' must be between 1 and {_MAX_LIMIT}."})
    if lookback < 1 or lookback > _MAX_SPAN:
        raise HTTPException(status_code=400, detail={"code": "BAD_LOOKBACK", "message": f"'lookback' must be between 1 and {_MAX_SPAN} blocks."})

    primary = ALCHEMY_BASE_URL or PUBLIC_BASE_RPC
    fallback_used = False
    url = primary

    # Resolve latest block if needed
    latest_hex, err = await _rpc(url, "eth_blockNumber", [])
    if err and ALCHEMY_BASE_URL and primary != PUBLIC_BASE_RPC:
        url, fallback_used = PUBLIC_BASE_RPC, True
        latest_hex, err = await _rpc(url, "eth_blockNumber", [])
    if err or not latest_hex:
        raise HTTPException(status_code=502, detail={"code": "RPC_UNAVAILABLE", "message": f"Base RPC unreachable ({err}); not charged."})
    latest = int(latest_hex, 16)

    hi = to_block if to_block is not None else latest
    lo = from_block if from_block is not None else max(0, hi - lookback)
    if hi < lo:
        raise HTTPException(status_code=400, detail={"code": "BAD_RANGE", "message": "to_block must be >= from_block."})
    if hi - lo > _MAX_SPAN:
        lo = hi - _MAX_SPAN  # clamp span, protect rate limit

    log_filter: dict[str, Any] = {"address": addr, "fromBlock": hex(lo), "toBlock": hex(hi)}
    if topic0:
        log_filter["topics"] = [topic0]

    result, err = await _rpc(url, "eth_getLogs", [log_filter])
    if err and not fallback_used and ALCHEMY_BASE_URL:
        url, fallback_used = PUBLIC_BASE_RPC, True
        result, err = await _rpc(url, "eth_getLogs", [log_filter])
    if err is not None:
        # Range-too-large / too-many-results style errors → 400 guidance, else 502
        low = err.lower()
        if "range" in low or "limit" in low or "too many" in low or "10000" in low:
            raise HTTPException(status_code=400, detail={"code": "RANGE_TOO_LARGE", "message": f"RPC rejected the query range ({err}); narrow from_block/to_block or lookback."})
        raise HTTPException(status_code=502, detail={"code": "RPC_ERROR", "message": f"eth_getLogs failed ({err}); not charged."})

    logs = result or []
    total = len(logs)
    logs = logs[-limit:] if total > limit else logs      # most recent within range
    decoded = [_decode_log(l) for l in logs]

    return {
        "query": {"contract": addr, "event": event or None, "topic0": topic0,
                  "from_block": lo, "to_block": hi, "lookback": lookback if from_block is None and to_block is None else None, "limit": limit},
        "block_range": {"from_block": lo, "to_block": hi, "latest_block": latest, "span": hi - lo},
        "count": len(decoded), "total_in_range": total, "truncated": total > limit,
        "events": decoded,
        "rpc": {"provider": "alchemy" if (url == ALCHEMY_BASE_URL and ALCHEMY_BASE_URL) else "public-base-rpc",
                "fallback_used": fallback_used},
        "decoders": sorted({v["name"] for v in _KNOWN.values()}),
        "source": SOURCE,
        "timestamp": utc_now(),
        "disclaimer": "Decoded from public on-chain logs; unknown event signatures are returned raw. Block span and result count are capped.",
        "cached": False,
    }


@router.get("/chain/events")
async def chain_events(
    contract: str = Query(..., description="Contract address to read logs from (0x + 40 hex)."),
    event: str | None = Query(None, description="Event name (Transfer/Approval/Swap/Mint/Burn/TransferSingle/Sync), full signature, or 0x topic0. Omit for all."),
    from_block: int | None = Query(None, description="Start block (inclusive). Omit to use lookback."),
    to_block: int | None = Query(None, description="End block (inclusive). Omit for latest."),
    lookback: int = Query(_DEFAULT_LOOKBACK, description=f"If from/to omitted, scan the last N blocks (1-{_MAX_SPAN}, default {_DEFAULT_LOOKBACK})."),
    limit: int = Query(25, description=f"Max events returned (1-{_MAX_LIMIT}, most recent in range)."),
) -> JSONResponse:
    """GET /chain/events — decoded, normalized on-chain events (eth_getLogs) for a contract on Base, with capped block span."""
    return JSONResponse(content=await fetch_events(contract, event, from_block, to_block, lookback, limit))


@router.get("/chain/events/health")
async def chain_events_health() -> JSONResponse:
    url = ALCHEMY_BASE_URL or PUBLIC_BASE_RPC
    latest, err = await _rpc(url, "eth_blockNumber", [])
    ok = err is None and latest is not None
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "chain-events", "status": "ok" if ok else "degraded",
        "rpc_configured": bool(ALCHEMY_BASE_URL), "latest_block": int(latest, 16) if ok else None,
        "upstream": {"reachable": ok, "detail": err or "HTTP 200"},
        "known_decoders": sorted({v["name"] for v in _KNOWN.values()})})
