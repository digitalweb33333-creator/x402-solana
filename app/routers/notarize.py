"""LOT 8 #3 — Notary / Proof-of-Existence ($0.01).

Give a SHA-256 hash (or short content hashed server-side) → get a timestamped,
Ed25519-signed proof-of-existence receipt, verifiable offline. Pure-local: no external
source, no key beyond RECEIPT_SIGNING_SEED (app/receipt.py). Deterministic.

HONEST positioning (cf RAPPORT-BENCHMARK-12): the incumbent AOTrust anchors hashes on
a blockchain (Merkle → NEAR). WE DO NOT anchor on-chain. This endpoint issues a *signed
proof-of-existence receipt*: the signature attests that this issuer saw `sha256` at
`issued_at`. `anchoring.anchored` is always False and the note says so plainly. A clean
extension point (`_anchor`) is left for a future periodic Merkle-root on-chain anchor.

The signature IS the product: if the signing seed is not configured, the endpoint returns
503 (middleware does not settle → agent not charged) rather than a worthless unsigned proof.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.receipt import receipt_available, sign_receipt
from app.verdict import now_iso

router = APIRouter()

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_CONTENT = 100_000  # chars hashed server-side
_MAX_MEMO = 200


class NotarizeRequest(BaseModel):
    hash: str | None = Field(None, description="SHA-256 hex digest (64 hex chars) to notarize.")
    content: str | None = Field(None, description="Short content to hash server-side (SHA-256) if you have no hash. Max 100000 chars.")
    memo: str | None = Field(None, description="Optional label bound into the signed proof, e.g. 'invoice #42'. Max 200 chars.")


def _anchor(sha256: str) -> dict[str, Any]:
    """Extension point for a future periodic Merkle-root on-chain anchor. Today: none.

    Kept as a function so a scheduled batch-anchor can later fill in
    {anchored: True, method: 'merkle-root', chain, tx, root, proof_path} without
    changing the response shape or the signed-claims contract.
    """
    return {
        "anchored": False,
        "method": None,
        "note": "Signed proof-of-existence receipt (Ed25519), NOT a blockchain-anchored timestamp. "
                "The signature attests this issuer observed the hash at issued_at. On-chain Merkle "
                "anchoring is a planned extension and is not performed here.",
    }


def _notarize(hash_in: str | None, content: str | None, memo: str | None) -> dict[str, Any]:
    if not receipt_available():
        raise HTTPException(status_code=503, detail={"code": "SIGNING_UNAVAILABLE",
                            "message": "Notary signing key not configured on this host; proof cannot be signed. Not charged."})

    hashed_server_side = False
    if hash_in and hash_in.strip():
        digest = hash_in.strip().lower()
        if not _SHA256_RE.match(digest):
            raise HTTPException(status_code=400, detail={"code": "BAD_HASH", "message": "'hash' must be a 64-char hex SHA-256 digest."})
    elif content is not None and content != "":
        if len(content) > _MAX_CONTENT:
            raise HTTPException(status_code=400, detail={"code": "CONTENT_TOO_LONG", "message": f"'content' must be <= {_MAX_CONTENT} chars."})
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        hashed_server_side = True
    else:
        raise HTTPException(status_code=400, detail={"code": "NOTHING_TO_NOTARIZE", "message": "Provide either 'hash' (SHA-256 hex) or 'content' to hash server-side."})

    if memo is not None and len(memo) > _MAX_MEMO:
        raise HTTPException(status_code=400, detail={"code": "MEMO_TOO_LONG", "message": f"'memo' must be <= {_MAX_MEMO} chars."})

    receipt = sign_receipt({
        "kind": "proof_of_existence",
        "algorithm": "sha256",
        "sha256": digest,
        "memo": (memo or None),
    })

    return {
        "proof": {
            "sha256": digest,
            "hash_algorithm": "sha256",
            "hashed_server_side": hashed_server_side,
            "memo": memo or None,
            "notarized_at": receipt.get("claims", {}).get("issued_at") if receipt.get("available") else now_iso(),
        },
        "signed_receipt": receipt,
        "anchoring": _anchor(digest),
        "verification": {
            "how": "Recompute sha256(content); confirm it equals proof.sha256; then ed25519-verify signed_receipt.signature "
                   "over canonical_json(signed_receipt.claims) with signed_receipt.public_key.",
            "offline": True,
        },
        "source": "Local Ed25519 proof-of-existence (no external source)",
        "timestamp": now_iso(),
        "disclaimer": "Signed proof that this issuer observed the given hash at the stated time. NOT a blockchain-anchored "
                      "timestamp and NOT a legal/qualified electronic timestamp (eIDAS/RFC 3161). The issuer never sees the "
                      "original content when only a hash is supplied.",
        "cached": False,
    }


@router.post("/proof/notarize")
async def notarize(req: NotarizeRequest) -> JSONResponse:
    """POST /proof/notarize — timestamp + Ed25519 signature of a SHA-256 hash (or content hashed server-side). Offline-verifiable proof-of-existence receipt."""
    return JSONResponse(content=_notarize(req.hash, req.content, req.memo))


@router.get("/proof/notarize")
async def notarize_get(
    hash: str | None = Query(None, description="SHA-256 hex digest (64 hex) to notarize."),
    content: str | None = Query(None, description="Short content to hash server-side instead of a hash."),
    memo: str | None = Query(None, description="Optional label bound into the signed proof."),
) -> JSONResponse:
    """GET /proof/notarize — same as POST (GET variant for Bazaar discovery / small hashes via query)."""
    return JSONResponse(content=_notarize(hash, content, memo))


@router.get("/proof/notarize/health")
async def notarize_health() -> JSONResponse:
    from app.receipt import public_key_hex
    ok = receipt_available()
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "notarize", "status": "ok" if ok else "degraded",
        "signing_available": ok, "public_key": public_key_hex(),
        "anchoring": "none (signed proof-of-existence only; on-chain Merkle anchor is a planned extension)"})
