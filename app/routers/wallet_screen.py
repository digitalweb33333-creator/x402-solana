"""Endpoint 3 — Wallet Sanctions & Compliance Screen (officiel, multi-chain).

Screening compliance d'un wallet (EVM + Solana + Bitcoin + Tron + XRP) contre les
listes de sanctions officielles publiques, avec exposition mixer, âge du wallet, et —
le DIFFÉRENCIATEUR — un REÇU DE CONFORMITÉ SIGNÉ (Ed25519) que l'agent FSI archive
comme pièce d'audit reproductible (SOX / model-risk), épinglé à une empreinte de la
version de liste.

Angle (cf benchmark) : GlobalAPI screen déjà multi-chain à $0.002, commoditisé.
PERSONNE ne renvoie un reçu signé/horodaté vérifiable hors-ligne. On mène avec le reçu.

5 règles : verdict PASS/WARN/BLOCK (+ABSTAIN) en haut, confidence + reasons[],
data_freshness/deterministic/sources, codes d'erreur, ABSTAIN si listes indisponibles.

Sources : OFAC SDN crypto (seule source officielle publiant des ADRESSES) + UN/UK
déclarées names_only + labels mixer Tornado. Tier $0.005. TTL 5 min.
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import sign_receipt
from app.sources.base_chain import BLOCKSCOUT_HOSTS, evm_counterparties
from app.sources.http_util import TTLCache
from app.sources.sanctions_screen import (
    TORNADO_CASH, chain_of, direct_screen, sanctioned_sets,
)
from app.sources.solana_rpc import solana_first_seen
from app.verdict import age_seconds, clamp01, freshness, now_iso, reason

router = APIRouter()

SOURCES = ["OFAC SDN crypto addresses (official, public)", "UN Security Council consolidated list (names)",
           "UK OFSI/FCDO consolidated list (names)", "Tornado Cash mixer labels", "Blockscout (EVM exposure & age)"]
_cache = TTLCache(300)

# UN / UK ne publient PAS d'adresses crypto → déclarées mais coverage names_only.
LISTS_DECLARED = [
    {"list": "OFAC SDN (crypto addresses)", "coverage": "addresses", "official": True},
    {"list": "UN Security Council consolidated", "coverage": "names_only", "official": True},
    {"list": "UK OFSI/FCDO consolidated", "coverage": "names_only", "official": True},
]


def _list_fingerprint(evm: set[str], other: set[str]) -> str:
    """Empreinte déterministe de la version de liste (épingle le reçu à une version)."""
    h = hashlib.sha256()
    for a in sorted(evm):
        h.update(a.encode())
    h.update(b"|")
    for a in sorted(other):
        h.update(a.encode())
    return h.hexdigest()[:16]


async def _mixer_exposure(addr: str, detected: str, evm_set: set[str], chains: list[str]) -> dict[str, Any]:
    """Exposition 1-hop à un mixer / une adresse sanctionnée (best-effort, EVM)."""
    if detected != "evm":
        return {"checked": False, "reason": "exposure analysis is EVM-only", "exposed": None, "hits": []}
    hosts = []
    for ch in chains or ["ethereum", "base"]:
        host = BLOCKSCOUT_HOSTS.get(ch.lower())
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        hosts = [BLOCKSCOUT_HOSTS["ethereum"], BLOCKSCOUT_HOSTS["base"]]
    flagged = TORNADO_CASH | evm_set
    hits: list[str] = []
    checked = False
    for host in hosts[:2]:
        parties, err = await evm_counterparties(addr, host, max_pages=2)
        if parties is None:
            continue
        checked = True
        hits.extend(sorted(parties & flagged))
    return {"checked": checked, "exposed": bool(hits) if checked else None,
            "hits": sorted(set(hits))[:20], "hosts_checked": hosts[:2]}


async def _wallet_age(addr: str, detected: str, chains: list[str]) -> dict[str, Any]:
    """Âge (borne INFÉRIEURE, best-effort) — première activité observable."""
    first = None
    if detected == "evm":
        host = next((BLOCKSCOUT_HOSTS.get(c.lower()) for c in (chains or []) if BLOCKSCOUT_HOSTS.get(c.lower())),
                    BLOCKSCOUT_HOSTS["ethereum"])
        parties, _ = await evm_counterparties(addr, host, max_pages=1)  # touch to ensure host live
        # On approxime via le 1er transfert visible — borne inférieure.
        from app.sources.base_chain import usdc_transfers_in  # noqa: import local (Base only)
        if host == BLOCKSCOUT_HOSTS["base"]:
            tr, _ = await usdc_transfers_in(addr, max_pages=10)
            if tr:
                first = min((t["ts"] for t in tr if t.get("ts")), default=None)
    elif detected == "solana":
        first = await solana_first_seen(addr)
    age_s = age_seconds(first)
    return {"first_seen_observed": first, "min_age_days": round(age_s / 86400.0, 1) if age_s is not None else None,
            "is_lower_bound": True}


async def screen(wallet: str, chains: list[str]) -> dict[str, Any]:
    addr = (wallet or "").strip()
    detected = chain_of(addr)
    if detected == "unknown":
        raise HTTPException(status_code=400, detail={"code": "UNRECOGNIZED_ADDRESS",
                            "message": "'wallet' is not a recognizable EVM/Solana/BTC/TRON/XRP address."})
    key = f"{addr}|{','.join(sorted(chains))}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    evm_set, other_set, loaded = await sanctioned_sets()
    list_ok = bool(evm_set or other_set)
    ds = await direct_screen(addr)
    exposure = await _mixer_exposure(addr, detected, evm_set, chains)
    age = await _wallet_age(addr, detected, chains)

    reasons: list[dict] = []
    matched_lists: list[dict] = []
    if ds["listed"]:
        matched_lists.append({"list": "OFAC SDN (crypto addresses)", "match": "direct", "official": True})
        reasons.append(reason("OFAC_DIRECT_MATCH", "Wallet is directly on the OFAC SDN crypto list", 1.0))
    if ds["is_known_mixer"]:
        matched_lists.append({"list": "OFAC sanctioned mixer (Tornado Cash)", "match": "direct", "official": True})
        reasons.append(reason("SANCTIONED_MIXER", "Wallet is a sanctioned mixer contract", 0.95))
    if exposure.get("exposed"):
        reasons.append(reason("MIXER_OR_SANCTIONED_EXPOSURE",
                              f"1-hop interaction with {len(exposure['hits'])} sanctioned/mixer address(es)", 0.6))

    # --- Verdict ---
    if not list_ok:
        verdict, confidence, error = "ABSTAIN", 0.3, {
            "code": "SANCTIONS_LISTS_UNAVAILABLE",
            "message": "Could not load OFAC lists; cannot issue a clearance verdict."}
    elif ds["listed"] or ds["is_known_mixer"]:
        verdict, confidence, error = "BLOCK", 0.98, None
    elif exposure.get("exposed"):
        verdict, confidence, error = "WARN", 0.7, None
    else:
        verdict, confidence, error = "PASS", clamp01(0.85 + (0.1 if exposure.get("checked") else 0.0)), None
        reasons.append(reason("NO_DIRECT_MATCH", "No match on any screened sanctions list", -0.5))

    fingerprint = _list_fingerprint(evm_set, other_set)
    claims = {
        "kind": "wallet_compliance_screen",
        "wallet": addr,
        "detected_chain": detected,
        "verdict": verdict,
        "matched_lists": [m["list"] for m in matched_lists],
        "ofac_list_fingerprint": fingerprint,
        "ofac_evm_size": len(evm_set), "ofac_other_size": len(other_set),
        "screened_at": now_iso(),
    }
    receipt = sign_receipt(claims)

    shaped = {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "query": {"wallet": addr, "chains": chains, "detected_chain": detected},
        "matched_lists": matched_lists,
        "lists_checked": LISTS_DECLARED,
        "mixer_exposure": exposure,
        "wallet_age": age,
        "signed_compliance_receipt": receipt,
        "data_freshness": freshness(now_iso(), deterministic=True, sources=SOURCES,
                                    extra={"ofac_lists_loaded": loaded, "ofac_list_fingerprint": fingerprint,
                                           "ofac_evm_size": len(evm_set), "ofac_other_size": len(other_set)}),
        "error": error,
        "timestamp": now_iso(),
        "disclaimer": "Screening against official OFAC crypto addresses; UN/UK lists are names-only (not address-screenable). "
                      "A clear result is not legal clearance. Not a compliance opinion.",
    }
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/compliance/wallet-screen")
async def wallet_screen(
    wallet: str = Query(..., description="Wallet to screen — EVM '0x..', Solana, BTC, TRON 'T..', XRP 'r..'"),
    chains: str | None = Query(None, description="Optional comma-separated EVM chains for exposure/age, e.g. 'ethereum,base'"),
) -> JSONResponse:
    """GET /compliance/wallet-screen — OFAC/UN/UK sanctions + mixer exposure + age + signed compliance receipt."""
    chain_list = [c.strip() for c in (chains or "").split(",") if c.strip()]
    return JSONResponse(content=await screen(wallet, chain_list))


@router.get("/compliance/wallet-screen/health")
async def wallet_screen_health() -> JSONResponse:
    evm, other, loaded = await sanctioned_sets()
    ok = bool(evm or other)
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "wallet-screen", "status": "ok" if ok else "degraded",
        "upstream": {"ofac_lists_loaded": loaded, "evm_size": len(evm), "other_size": len(other),
                     "receipt_signing": __import__("app.receipt", fromlist=["receipt_available"]).receipt_available()},
        "cache_entries": len(_cache)})
