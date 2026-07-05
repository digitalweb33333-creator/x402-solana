"""Reçus signés Ed25519 — pièces d'audit vérifiables hors-ligne par l'agent.

Plusieurs endpoints « méta-confiance / compliance » (seller-trust, wallet-screen)
renvoient un `signed_receipt` : un objet {claims, issuer, issued_at, public_key,
algorithm, signature} où `signature` couvre le JSON canonique (clés triées,
séparateurs compacts) des `claims`. L'agent peut donc archiver le reçu et le
prouver plus tard sans rappeler l'API (audit SOX / model-risk).

Clé : seed Ed25519 32 octets en hex dans l'env `RECEIPT_SIGNING_SEED`. La clé
PUBLIQUE (vérification) est publiée dans agent-card.json et /.well-known/receipt-pubkey.json.
Si la seed est absente, `sign_receipt` renvoie un stub `{"available": false, ...}`
pour ne JAMAIS casser la réponse (l'endpoint garde son verdict).

Réutilise PyNaCl (déjà dépendance du SDK x402, cf app/cdp_auth.py) — zéro dépendance ajoutée.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from dotenv import load_dotenv
from nacl.signing import SigningKey

# Charge .env si pas déjà fait (robuste à l'ordre d'import ; sur Render l'env est natif).
load_dotenv()

_ISSUER = "x402-solana.onrender.com"
_ALGO = "ed25519"

_seed_hex = (os.getenv("RECEIPT_SIGNING_SEED", "") or "").strip()
_signing_key: SigningKey | None = None
_public_key_hex: str | None = None
if _seed_hex:
    try:
        _signing_key = SigningKey(bytes.fromhex(_seed_hex))
        _public_key_hex = _signing_key.verify_key.encode().hex()
    except (ValueError, TypeError):  # seed malformée -> reçus désactivés proprement
        _signing_key = None


def receipt_available() -> bool:
    return _signing_key is not None


def public_key_hex() -> str | None:
    return _public_key_hex


def _canonical(obj: Any) -> bytes:
    """JSON canonique déterministe : clés triées, séparateurs compacts, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_receipt(claims: dict[str, Any]) -> dict[str, Any]:
    """Signe `claims` et renvoie le reçu vérifiable hors-ligne.

    `claims` doit contenir les faits matériels du verdict (wallet, verdict, score,
    timestamp…). On y ajoute `issuer` et `issued_at` AVANT de signer pour qu'ils
    soient couverts par la signature.
    """
    if _signing_key is None:
        return {"available": False, "reason": "RECEIPT_SIGNING_SEED not configured on this host"}
    body = dict(claims)
    body.setdefault("issuer", _ISSUER)
    body.setdefault("issued_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    message = _canonical(body)
    signature = _signing_key.sign(message).signature
    return {
        "available": True,
        "claims": body,
        "algorithm": _ALGO,
        "public_key": _public_key_hex,
        "canonicalization": "JSON sort_keys, separators (',',':'), UTF-8",
        "signature": signature.hex(),
        "verify_hint": "ed25519_verify(public_key, signature, canonical_json(claims))",
    }


def verify_receipt(receipt: dict[str, Any]) -> bool:
    """Vérifie un reçu (utilisé par les tests ; un agent ferait pareil hors-ligne)."""
    from nacl.signing import VerifyKey

    try:
        vk = VerifyKey(bytes.fromhex(receipt["public_key"]))
        vk.verify(_canonical(receipt["claims"]), bytes.fromhex(receipt["signature"]))
        return True
    except Exception:
        return False
