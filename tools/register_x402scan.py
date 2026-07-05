"""Enregistre l'origine sur x402scan via SIWX (signature wallet, PAS un paiement).

POST register-origin -> challenge SIWX (402) -> signe avec le wallet buyer Solana
(Ed25519) -> re-POST avec le header SIGN-IN-WITH-X. x402scan crawle ensuite
/.well-known/x402 (URL live) et indexe les 10 endpoints. Aucun USDC ne bouge.

Si le challenge demande une chaîne EVM, bascule sur eth_account (nécessite une clé
EVM dans EVM_SIGNER_KEY ; sinon signale le blocage).
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx

from x402.extensions.sign_in_with_x import (
    SIGN_IN_WITH_X,
    create_siwx_payload,
    encode_siwx_header,
)

ORIGIN = os.getenv("ORIGIN", "https://x402-solana-cva8.onrender.com")
REG = "https://www.x402scan.com/api/x402/registry/register-origin"
BUYER_KEY = os.environ["SOLANA_BUYER_PRIVATE_KEY"]


def _solana_signer():
    from x402.mechanisms.svm.signers import KeypairSigner
    return KeypairSigner.from_base58(BUYER_KEY)


def _evm_key_from_base_env() -> str | None:
    # Lit BUYER_PRIVATE_KEY (EVM) du .env Base à l'exécution — signature d'identité
    # SIWX uniquement (aucun fonds), jamais écrit/commité côté Solana.
    path = "/home/joachim/x402-endpoints/.env"
    try:
        for line in open(path, encoding="utf-8"):
            if line.strip().startswith("BUYER_PRIVATE_KEY="):
                return line.strip().split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _evm_signer():
    from eth_account import Account
    key = os.getenv("EVM_SIGNER_KEY") or os.getenv("BUYER_PRIVATE_KEY") or _evm_key_from_base_env()
    if not key:
        return None
    return Account.from_key(key)


async def main() -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(220.0, connect=15.0)) as c:
        # 0) alias /.well-known/x402 live ?
        r0 = await c.get(ORIGIN + "/.well-known/x402")
        print(f"/.well-known/x402 -> {r0.status_code}")
        # 1) challenge
        r1 = await c.post(REG, json={"origin": ORIGIN}, headers={"Content-Type": "application/json"})
        if r1.status_code != 402:
            print("pas de challenge SIWX:", r1.status_code, r1.text[:300]); return 1
        info = r1.json()["extensions"]["sign-in-with-x"]["info"]
        chain_id = info.get("chainId") or info.get("chain_id") or ""
        print("challenge chainId:", chain_id, "| nonce:", info.get("nonce"))
        # 2) choisir le signer selon la chaîne
        if str(chain_id).startswith("solana:"):
            signer = _solana_signer()
            print("signer Solana:", signer.address)
        else:
            signer = _evm_signer()
            if signer is None:
                print(f"BLOCAGE: challenge EVM ({chain_id}) mais aucune clé EVM disponible (EVM_SIGNER_KEY)."); return 2
            print("signer EVM:", signer.address)
        # 3) signer + encoder
        payload = await create_siwx_payload(info, signer)
        header_val = encode_siwx_header(payload)
        # 4) re-POST authentifié
        r2 = await c.post(REG, json={"origin": ORIGIN},
                          headers={"Content-Type": "application/json", SIGN_IN_WITH_X: header_val})
        print("\nregister-origin authed -> HTTP", r2.status_code)
        try:
            print(json.dumps(r2.json(), indent=2)[:1200])
        except Exception:
            print(r2.text[:800])
        return 0 if r2.status_code < 300 else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
