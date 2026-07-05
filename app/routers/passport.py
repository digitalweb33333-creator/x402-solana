"""PREMIUM-3 #3 — Agent Passport ($1.00): measured-vs-declared reputation, Sybil-corrected.

NOT a reputation "score lookup" (commoditised at ~$0.001 by AgentStamp/ClawTrust): an
audit-grade due-diligence dossier whose PRODUCT is the GAP between what an agent's
ERC-8004 record CLAIMS and what the chain actually SUPPORTS. Peer research (arXiv:2606.26028)
finds ERC-8004 reputation on Base is ~90% Sybil-inflated — correcting that gap IS the moat.

Reads (all via eth_call on ALCHEMY_BASE_URL — free-tier safe, no getLogs range needed):
  IdentityRegistry 0x8004A169…  : ownerOf, tokenURI, getAgentWallet, balanceOf(owner)
  ReputationRegistry 0x8004BAa1…: getClients, readAllFeedback, getSummary
Reverse (wallet -> agentId) via Blockscout `Registered` logs (keyless, wide range).

DECLARED reputation = how good the ERC-8004 record looks (review count/reviewers/avg value).
MEASURED reputation = declared discounted by DETERMINISTIC Sybil signals + real settlement
backing (via /x402/seller-trust on the operator wallet). DELTA = declared - measured.
Claude/Haiku only writes the narrative; every score/delta/verdict is computed by code and
signed Ed25519 (verifiable offline). No new key. If a required key is missing -> 502, no mock.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import httpx
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import ALCHEMY_BASE_URL
from app.llm import compose, llm_available
from app.receipt import sign_receipt
from app.routers import seller_trust
from app.sources.http_util import TTLCache, client, get_json
from app.verdict import clamp01, freshness, now_iso, reason

router = APIRouter()

ID_REG = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
REP_REG = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
PUBLIC_BASE_RPC = "https://mainnet.base.org"
BLOCKSCOUT = "https://base.blockscout.com"

SOURCES = [
    "ERC-8004 IdentityRegistry + ReputationRegistry on Base (eth_call)",
    "Blockscout (wallet->agentId reverse via Registered logs; reviewer footprint)",
    "Internal /x402/seller-trust (real settlement backing)",
]
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(300)
_PLACEHOLDER_TAGS = {"", "test", "demo", "sample", "todo", "foo", "bar"}
_MAX_REVIEWER_SAMPLE = 8


# ---------------------------------------------------------------- eth_call
async def _eth_call(sig: str, argtypes: list[str], args: list, outtypes: list[str]) -> tuple[Any, str | None]:
    """Encode + eth_call (ALCHEMY primary, public Base RPC fallback) + decode. Never raises."""
    data = "0x" + (keccak(text=sig)[:4] + (abi_encode(argtypes, args) if argtypes else b"")).hex()
    to = ID_REG if sig.split("(")[0] in ("ownerOf", "tokenURI", "getAgentWallet", "balanceOf", "name", "symbol") else REP_REG
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
    c = await client("erc8004", timeout=20.0)
    for url in [u for u in (ALCHEMY_BASE_URL, PUBLIC_BASE_RPC) if u]:
        try:
            r = await c.post(url, json=payload)
            j = r.json()
        except Exception as exc:
            last = type(exc).__name__
            continue
        if j.get("error"):
            return None, str(j["error"].get("message", j["error"]))[:120]
        res = j.get("result")
        if not res or res == "0x":
            return None, "empty"
        try:
            out = abi_decode(outtypes, bytes.fromhex(res[2:]))
            return out, None
        except Exception as exc:
            return None, f"decode_{type(exc).__name__}"
    return None, "rpc_unreachable"


async def _wallet_to_agent_ids(wallet: str) -> tuple[list[int], str | None]:
    """Reverse lookup: which ERC-8004 agent NFTs does this wallet own? Via Blockscout NFT
    holdings (keyless, robust). The IdentityRegistry is an ERC-721; the tokenId IS the agentId."""
    c = await client("blockscout", timeout=15.0)
    url = f"{BLOCKSCOUT}/api/v2/addresses/{wallet}/nft"
    params: dict = {"type": "ERC-721"}
    ids: list[int] = []
    last_err = None
    for _ in range(3):  # follow up to 3 pages to find the ERC-8004 collection among the wallet's NFTs
        data, err = await get_json(c, url, params=params, attempts=2)
        if err or not isinstance(data, dict):
            last_err = err or "bad_shape"
            break
        for it in (data.get("items") or []):
            tok = (it.get("token") or {})
            addr = (tok.get("address") or tok.get("address_hash") or "")
            if addr.lower() == ID_REG.lower():
                try:
                    ids.append(int(it.get("id")))
                except (TypeError, ValueError):
                    pass
        nxt = data.get("next_page_params")
        if ids or not nxt:
            break
        params = {"type": "ERC-721", **nxt}
    return sorted(set(ids)), (None if ids else last_err)


# ---------------------------------------------------------------- reads
async def _read_identity(agent_id: int) -> dict[str, Any]:
    owner, _ = await _eth_call("ownerOf(uint256)", ["uint256"], [agent_id], ["address"])
    if owner is None:
        return {"exists": False}
    uri, _ = await _eth_call("tokenURI(uint256)", ["uint256"], [agent_id], ["string"])
    op, _ = await _eth_call("getAgentWallet(uint256)", ["uint256"], [agent_id], ["address"])
    owner_addr = owner[0]
    bal, _ = await _eth_call("balanceOf(address)", ["address"], [owner_addr], ["uint256"])
    operator = op[0] if op else None
    if operator and int(operator, 16) == 0:
        operator = None
    return {"exists": True, "agent_id": agent_id, "owner": owner_addr,
            "operator_wallet": operator, "registration_uri": uri[0] if uri else None,
            "agents_owned_by_owner": int(bal[0]) if bal else None}


async def _read_reputation(agent_id: int) -> dict[str, Any]:
    clients_res, _ = await _eth_call("getClients(uint256)", ["uint256"], [agent_id], ["address[]"])
    clients = list(clients_res[0]) if clients_res else []
    fb, _ = await _eth_call(
        "readAllFeedback(uint256,address[],string,string,bool)",
        ["uint256", "address[]", "string", "string", "bool"], [agent_id, [], "", "", True],
        ["address[]", "uint64[]", "int128[]", "uint8[]", "string[]", "string[]", "bool[]"])
    if fb is None:
        return {"available": bool(clients), "distinct_reviewers": len(set(clients)),
                "feedback_count": 0, "feedbacks": [], "reviewers": clients}
    fb_clients, idxs, values, decs, tag1, tag2, revoked = fb
    feedbacks = []
    for i in range(len(values)):
        feedbacks.append({"client": fb_clients[i], "value": int(values[i]), "decimals": int(decs[i]),
                          "scaled": int(values[i]) / (10 ** int(decs[i])) if int(decs[i]) else int(values[i]),
                          "tag1": tag1[i], "tag2": tag2[i], "revoked": bool(revoked[i])})
    return {"available": True, "reviewers": clients, "distinct_reviewers": len(set(fb_clients or clients)),
            "feedback_count": len(feedbacks), "feedbacks": feedbacks}


async def _reviewer_footprint(reviewers: list[str]) -> dict[str, Any]:
    """Best-effort: fraction of sampled reviewers with no economic footprint (burner/Sybil signal)."""
    sample = list(dict.fromkeys(reviewers))[:_MAX_REVIEWER_SAMPLE]
    if not sample:
        return {"available": False, "sampled": 0}
    c = await client("blockscout", timeout=12.0)
    dead = 0
    checked = 0
    for addr in sample:
        data, err = await get_json(c, f"{BLOCKSCOUT}/api/v2/addresses/{addr}", attempts=1)
        if err or not isinstance(data, dict):
            continue
        checked += 1
        bal = data.get("coin_balance")
        has_tokens = bool(data.get("has_tokens") or data.get("has_token_transfers"))
        if (bal in (None, "0", 0)) and not has_tokens:
            dead += 1
    if not checked:
        return {"available": False, "sampled": len(sample)}
    return {"available": True, "sampled": checked, "no_footprint": dead,
            "no_footprint_ratio": round(dead / checked, 3)}


# ---------------------------------------------------------------- measurement (deterministic)
def _declared_score(rep: dict) -> tuple[int, dict]:
    reviewers = rep.get("distinct_reviewers", 0)
    count = rep.get("feedback_count", 0)
    live = [f for f in rep.get("feedbacks", []) if not f["revoked"]]
    avg = round(sum(f["scaled"] for f in live) / len(live), 3) if live else None
    # How reputable the ERC-8004 record LOOKS (this is exactly what a naive reader trusts).
    score = int(clamp01((min(60, reviewers * 4) + min(40, count * 1.0)) / 100.0) * 100)
    return score, {"distinct_reviewers": reviewers, "feedback_count": count,
                   "live_feedback_count": len(live), "avg_value_scaled": avg}


def _sybil_signals(ident: dict, rep: dict, footprint: dict, backing: dict) -> dict[str, Any]:
    sig: dict[str, dict] = {}
    live = [f for f in rep.get("feedbacks", []) if not f["revoked"]]
    fb_count = len(live) or rep.get("feedback_count", 0)
    reviewers = rep.get("distinct_reviewers", 0)

    owned = ident.get("agents_owned_by_owner")
    if isinstance(owned, int):
        sig["owner_agent_farm"] = {"value": round(clamp01((owned - 1) / 10.0), 3),
                                   "detail": f"Owner holds {owned} agent identity NFT(s) (many => Sybil farm)."}
    if fb_count and reviewers:
        conc = 1 - reviewers / fb_count
        sig["review_concentration"] = {"value": round(clamp01(conc), 3),
                                       "detail": f"{fb_count} feedbacks from only {reviewers} distinct reviewers."}
    if live:
        vals = Counter(f["value"] for f in live)
        sig["value_uniformity"] = {"value": round(vals.most_common(1)[0][1] / len(live), 3),
                                   "detail": f"Most common rating value repeats in {vals.most_common(1)[0][1]}/{len(live)} feedbacks."}
        ph = sum(1 for f in live if (f["tag1"] or "").strip().lower() in _PLACEHOLDER_TAGS)
        sig["placeholder_tags"] = {"value": round(ph / len(live), 3),
                                   "detail": f"{ph}/{len(live)} feedbacks carry a placeholder/test tag."}
    if footprint.get("available"):
        sig["reviewer_no_footprint"] = {"value": footprint["no_footprint_ratio"],
                                        "detail": f"{footprint['no_footprint']}/{footprint['sampled']} sampled reviewers have no economic on-chain footprint (burner wallets)."}
    # unbacked: declared reputation exists but the operator has ~no real settlement history
    sc = backing.get("settlement_count")
    declared_present = fb_count >= 3
    if declared_present:
        unbacked = 1.0 if (sc is None or sc < 3) else clamp01((3 - min(sc, 3)) / 3.0)
        sig["unbacked_by_settlement"] = {"value": round(unbacked, 3),
                                         "detail": f"Operator wallet shows {sc if sc is not None else 'no'} real on-chain settlement(s) to back {fb_count} declared feedbacks."}
    return sig


_WEIGHTS = {"owner_agent_farm": 0.22, "unbacked_by_settlement": 0.24, "reviewer_no_footprint": 0.2,
            "review_concentration": 0.12, "value_uniformity": 0.12, "placeholder_tags": 0.10}


def _penalty(sig: dict) -> float:
    present = {k: v["value"] for k, v in sig.items() if k in _WEIGHTS}
    if not present:
        return 0.0
    tw = sum(_WEIGHTS[k] for k in present)
    return clamp01(sum(_WEIGHTS[k] * present[k] for k in present) / tw)


# ---------------------------------------------------------------- narrative (presentation only)
_SUM_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "description": "3-5 sentence plain-language read of the gap between the agent's DECLARED ERC-8004 reputation and its MEASURED, Sybil-corrected reputation, and the verdict."}}, "required": ["summary"], "additionalProperties": False}


async def _summary(facts: dict) -> tuple[str, str]:
    base = (f"Declared reputation {facts['declared']}/100 vs measured {facts['measured']}/100 "
            f"(delta {facts['delta']}). Verdict {facts['verdict']}. {facts['headline']}")
    if not llm_available():
        return base, "heuristic"
    sys = ("You explain an ERC-8004 agent due-diligence dossier to an agent about to engage this counterparty. Use ONLY "
           "the provided numbers. The core finding is the GAP between DECLARED and MEASURED (Sybil-corrected) reputation. "
           "Never invent numbers. Be concrete about which Sybil signals drove the gap. 3-5 sentences.")
    user = (f"DECLARED {facts['declared']}/100 | MEASURED {facts['measured']}/100 | DELTA {facts['delta']} | VERDICT {facts['verdict']}\n"
            f"SYBIL SIGNALS: {facts['signals']}\nSETTLEMENT BACKING: {facts['backing']}\nDECLARED DETAIL: {facts['declared_detail']}")
    out, err = await compose(system=sys, user=user, schema=_SUM_SCHEMA, tool_description="Emit the summary.", max_tokens=420)
    if out and out.get("summary"):
        return out["summary"], "llm"
    return base, f"heuristic({err})"


# ---------------------------------------------------------------- main
async def passport(agent_id: int | None, wallet: str | None) -> dict[str, Any]:
    # RPC: prefer ALCHEMY_BASE_URL, fall back to the public Base RPC (_eth_call handles both).
    # No hard requirement on Alchemy — the public RPC keeps the endpoint live where Alchemy is unset.
    w = (wallet or "").strip()
    if agent_id is None and not w:
        raise HTTPException(status_code=400, detail={"code": "INPUT_REQUIRED", "message": "Provide 'agent_id' (ERC-8004 tokenId) or 'wallet' (0x + 40 hex)."})
    if w and not _ADDR_RE.match(w):
        raise HTTPException(status_code=400, detail={"code": "BAD_WALLET", "message": "'wallet' must be an EVM address (0x + 40 hex)."})
    if agent_id is not None and agent_id < 0:
        raise HTTPException(status_code=400, detail={"code": "BAD_AGENT_ID", "message": "'agent_id' must be a non-negative integer."})

    resolved_via = "agent_id"
    resolved_ids: list[int] = []
    reverse_err = None
    if agent_id is not None:
        resolved_ids = [agent_id]
    else:
        resolved_ids, reverse_err = await _wallet_to_agent_ids(w)
        resolved_via = "wallet->Registered logs"

    # --- No ERC-8004 identity: measured-only path (still useful, honest) ---
    if not resolved_ids:
        backing = await _measured_only(w)
        as_of = now_iso()
        receipt = sign_receipt({"kind": "agent_passport", "agent_id": None, "wallet": w or None,
                                "erc8004_identity": False, "declared_score": None,
                                "measured_score": backing.get("trust_score"), "delta": None,
                                "verdict": backing.get("verdict"), "as_of": as_of})
        return {"verdict": backing.get("verdict") or "ABSTAIN", "erc8004_identity": False,
                "declared": {"available": False, "reason": reverse_err or "no ERC-8004 agent registered for this wallet"},
                "measured": {"available": backing.get("available"), "score": backing.get("trust_score"),
                             "settlement_backing": backing},
                "delta": None,
                "summary": ("No ERC-8004 identity found for this wallet — reputation is MEASURED-only from on-chain "
                            f"settlement activity ({backing.get('verdict')})."),
                "query": {"agent_id": None, "wallet": w or None, "resolved_via": resolved_via},
                "signed_receipt": receipt,
                "data_freshness": freshness(as_of, deterministic=True, sources=SOURCES),
                "error": None, "timestamp": as_of,
                "disclaimer": "No ERC-8004 record; measured-only reputation from Base settlement graph. Not financial/compliance advice.",
                "cached": False}

    # --- Full passport on the primary agentId ---
    aid = resolved_ids[0]
    ident = await _read_identity(aid)
    if not ident.get("exists"):
        raise HTTPException(status_code=404, detail={"code": "AGENT_NOT_FOUND", "message": f"No ERC-8004 agent with id {aid} on Base."})
    rep = await _read_reputation(aid)

    backing_wallet = ident.get("operator_wallet") or ident.get("owner")
    backing = await _measured_only(backing_wallet) if backing_wallet else {"available": False}

    footprint = await _reviewer_footprint(rep.get("reviewers", [])) if rep.get("distinct_reviewers") else {"available": False, "sampled": 0}

    declared, declared_detail = _declared_score(rep)
    signals = _sybil_signals(ident, rep, footprint, backing)
    penalty = _penalty(signals)
    measured = int(round(declared * (1 - penalty)))
    # real settlement backing can lift measured off the floor if the operator is genuinely active
    if backing.get("available") and (backing.get("settlement_count") or 0) >= 3 and (backing.get("trust_score") or 0) > measured:
        measured = int(round(0.5 * measured + 0.5 * backing["trust_score"]))
    delta = declared - measured

    if declared < 12 and rep.get("feedback_count", 0) < 3:
        verdict = "THIN"          # not enough declared reputation to assess a gap
        headline = "Sparse ERC-8004 record — little declared reputation to verify."
    elif penalty >= 0.6 or delta >= 40:
        verdict = "INFLATED"
        headline = "Declared reputation is largely Sybil-inflated / unbacked — do not rely on it."
    elif delta >= 20:
        verdict = "REVIEW"
        headline = "Material gap between declared and measured reputation — verify before relying on it."
    else:
        verdict = "VERIFIED"
        headline = "Declared reputation is broadly backed by measured on-chain reality."

    reasons = [reason("DECLARED_VS_MEASURED", f"Declared {declared}/100 vs measured {measured}/100 (Sybil-corrected).", clamp01(delta / 100.0))]
    for k, v in sorted(signals.items(), key=lambda kv: -kv[1]["value"]):
        if v["value"] >= 0.3:
            reasons.append(reason(k.upper(), v["detail"], round(v["value"] * _WEIGHTS.get(k, 0.1), 3)))

    facts = {"declared": declared, "measured": measured, "delta": delta, "verdict": verdict, "headline": headline,
             "signals": {k: v["value"] for k, v in signals.items()}, "backing": {"verdict": backing.get("verdict"), "settlement_count": backing.get("settlement_count")},
             "declared_detail": declared_detail}
    summary, mode = await _summary(facts)

    as_of = now_iso()
    receipt = sign_receipt({
        "kind": "agent_passport", "agent_id": aid, "wallet": w or None,
        "owner": ident.get("owner"), "operator_wallet": ident.get("operator_wallet"),
        "erc8004_identity": True, "declared_score": declared, "measured_score": measured,
        "delta": delta, "sybil_penalty": round(penalty, 3), "verdict": verdict,
        "feedback_count": rep.get("feedback_count"), "distinct_reviewers": rep.get("distinct_reviewers"),
        "as_of": as_of,
    })

    return {
        "verdict": verdict,
        "headline": headline,
        "erc8004_identity": True,
        "declared": {"available": True, "score": declared, **declared_detail,
                     "note": "Reputation as the ERC-8004 ReputationRegistry presents it (review count/reviewers/values)."},
        "measured": {"available": True, "score": measured, "sybil_penalty": round(penalty, 3),
                     "note": "Declared reputation discounted by deterministic Sybil signals + real settlement backing."},
        "delta": {"value": delta, "interpretation": ("declared reputation is inflated vs measured reality" if delta >= 20 else "declared and measured are broadly consistent")},
        "sybil_signals": signals,
        "settlement_backing": backing,
        "reviewers_sampled": footprint,
        "identity": {"agent_id": aid, "owner": ident.get("owner"), "operator_wallet": ident.get("operator_wallet"),
                     "registration_uri": ident.get("registration_uri"), "agents_owned_by_owner": ident.get("agents_owned_by_owner"),
                     "all_agent_ids_for_wallet": resolved_ids if agent_id is None else None},
        "summary": summary,
        "reasons": reasons,
        "query": {"agent_id": aid, "wallet": w or None, "resolved_via": resolved_via},
        "signed_receipt": receipt,
        "data_freshness": freshness(as_of, deterministic=(mode != "llm"), sources=SOURCES,
                                    extra={"summary_mode": mode, "reviewer_footprint_available": footprint.get("available")}),
        "error": None,
        "source": " + ".join(SOURCES),
        "timestamp": as_of,
        "disclaimer": "Measured-vs-declared ERC-8004 reputation dossier. Scores/delta/verdict are computed deterministically "
                      "from on-chain data and signed; the summary is explanation only. Sybil correction is heuristic (ERC-8004 "
                      "reputation is widely Sybil-inflated, cf arXiv:2606.26028). Not legal/financial advice.",
        "cached": False,
    }


async def _measured_only(wallet: str | None) -> dict[str, Any]:
    if not wallet or not _ADDR_RE.match(wallet):
        return {"available": False, "reason": "no backing wallet"}
    try:
        t = await seller_trust.assess(wallet, "shallow")
        m = t.get("metrics") or {}
        return {"available": True, "verdict": t.get("verdict"), "trust_score": t.get("trust_score"),
                "settlement_count": m.get("settlement_count"), "unique_counterparties": m.get("unique_counterparties"),
                "wallet_age_days": m.get("wallet_age_days"), "wallet": wallet}
    except HTTPException as exc:
        return {"available": False, "reason": (exc.detail or {}).get("code") if isinstance(exc.detail, dict) else f"http_{exc.status_code}", "wallet": wallet}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__, "wallet": wallet}


@router.get("/agent/passport")
async def agent_passport(
    agent_id: int | None = Query(None, description="ERC-8004 agent identity tokenId (IdentityRegistry). Provide this OR wallet."),
    wallet: str | None = Query(None, description="Agent wallet (0x + 40 hex). Resolved to its ERC-8004 agentId via Registered logs. Provide this OR agent_id."),
) -> JSONResponse:
    """GET /agent/passport — measured-vs-declared ERC-8004 reputation dossier: declared reputation, Sybil-corrected measured reputation, the DELTA, the manipulation evidence, and a signed offline-verifiable verdict."""
    return JSONResponse(content=await passport(agent_id, wallet))


@router.get("/agent/passport/health")
async def passport_health() -> JSONResponse:
    from app.receipt import receipt_available
    return JSONResponse(status_code=200, content={
        "endpoint": "passport", "status": "ok",
        "reads": {"identity_registry": ID_REG, "reputation_registry": REP_REG},
        "rpc_configured": bool(ALCHEMY_BASE_URL), "llm_configured": llm_available(), "receipt_signing": receipt_available(),
        "note": "Scores/delta/verdict deterministic + signed; LLM writes the narrative only. eth_call getters (free-tier safe)."})
