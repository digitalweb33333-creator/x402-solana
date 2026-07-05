"""Settlement buyer -> endpoint x402 Solana.

DEUX MODES :
  (défaut) DRY  : GET -> 402 -> CONSTRUIT + SIGNE la transaction de paiement SPL
                  (transfer USDC ATA->ATA, feePayer facilitator) MAIS NE LA SOUMET PAS.
                  Aucune dépense on-chain. Valide que le buyer peut payer chaque endpoint.
  --execute EXEC : envoie aussi la requête payée -> le serveur appelle /settle du
                   facilitator -> transaction soumise on-chain. Affiche le tx hash.

Le settle réel n'a lieu QUE via la requête payée (le serveur relaie au facilitator).
Construire/signer localement ne dépense rien.

Préflight inclus : solde USDC du buyer, existence des ATA buyer & seller.

Usage :
  ./.venv/bin/python -m tools.settle                 # dry-run tous les endpoints
  ./.venv/bin/python -m tools.settle --only /gleif/lei
  ./.venv/bin/python -m tools.settle --execute --only /gleif/lei
  ./.venv/bin/python -m tools.settle --execute       # settle 1x chaque endpoint (APRÈS go)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import httpx

from app.config import SOLANA_RPC_URL, SOLANA_SELLER_ADDRESS, SOLANA_USDC_MINT

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
BUYER_ADDRESS = os.environ["SOLANA_BUYER_ADDRESS"]
BUYER_KEY = os.environ["SOLANA_BUYER_PRIVATE_KEY"]

# (path, query) — 1 settle par endpoint prioritaire
PLAN = [
    ("/gleif/lei", {"lei": "529900T8BM49AURSDO55"}),
    ("/sanctions/screen", {"name": "Saddam Hussein"}),
    ("/polymarket/odds", {"market": "2654605"}),
    ("/crypto/token-safety", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}),
    ("/crypto/pre-trade-verdict", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}),
    ("/crypto/token-dossier", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}),
    ("/solana/token-safety", {"mint": SOLANA_USDC_MINT}),
    ("/solana/pre-trade", {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"}),
    # seller réel & découvrable (discovery doc + présence Bazaar) pour que la
    # résolution de mots-clés/rank aboutisse (sinon 502 NO_CATEGORY, non chargé).
    ("/agent/rank-check", {"seller": "x402-endpoints.onrender.com"}),
    ("/agent/visibility-audit", {"seller": "x402-endpoints.onrender.com"}),
]


def _build_client():
    from x402 import x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.svm.exact import register_exact_svm_client
    from x402.mechanisms.svm.signers import KeypairSigner

    signer = KeypairSigner.from_base58(BUYER_KEY)
    core = x402ClientSync()
    register_exact_svm_client(core, signer, rpc_url=SOLANA_RPC_URL)
    return x402HTTPClientSync(core)


def _ata(owner: str, mint: str) -> str:
    from solders.pubkey import Pubkey
    from spl.token.instructions import get_associated_token_address
    return str(get_associated_token_address(Pubkey.from_string(owner), Pubkey.from_string(mint)))


def preflight() -> None:
    from solana.rpc.api import Client
    c = Client(SOLANA_RPC_URL)
    print("=== PREFLIGHT ===")
    print(f"buyer  : {BUYER_ADDRESS}")
    print(f"seller : {SOLANA_SELLER_ADDRESS}")
    for label, owner in (("buyer", BUYER_ADDRESS), ("seller", SOLANA_SELLER_ADDRESS)):
        ata = _ata(owner, SOLANA_USDC_MINT)
        try:
            bal = c.get_token_account_balance(__import__("solders.pubkey", fromlist=["Pubkey"]).Pubkey.from_string(ata))
            ui = bal.value.ui_amount_string
            print(f"  {label} USDC ATA {ata} -> balance {ui} USDC")
        except Exception as e:  # noqa: BLE001
            print(f"  {label} USDC ATA {ata} -> ABSENT or unreadable ({type(e).__name__})")
    print()


def _decode_settle_header(resp: httpx.Response) -> dict:
    for h in ("x-payment-response", "payment-response", "PAYMENT-RESPONSE"):
        raw = resp.headers.get(h)
        if raw:
            try:
                return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            except Exception:
                return {"raw": raw}
    return {}


def run(execute: bool, only: str | None) -> int:
    http_client = _build_client()
    plan = [(p, q) for p, q in PLAN if (only is None or p == only)]
    total = 0
    with httpx.Client(timeout=60.0) as c:
        for path, query in plan:
            url = f"{BASE}{path}"
            r = c.get(url, params=query)
            if r.status_code != 402:
                print(f"[{path}] attendu 402, reçu {r.status_code} — skip"); continue
            payment_headers, payload = http_client.handle_402_response(dict(r.headers), r.content)
            if payload is None:
                print(f"[{path}] hook a court-circuité le paiement — skip"); continue
            print(f"[{path}] paiement CONSTRUIT + SIGNÉ (payload prêt).")
            if not execute:
                print(f"          DRY: non soumis (aucune dépense).")
                continue
            paid = c.get(url, params=query, headers=payment_headers)
            settle = _decode_settle_header(paid)
            tx = settle.get("transaction") or settle.get("txHash") or settle.get("raw")
            ok = settle.get("success", paid.status_code == 200)
            print(f"          EXEC: HTTP {paid.status_code} success={ok} tx={tx}")
            if ok:
                total += 1
    print(f"\n=== {'EXEC' if execute else 'DRY'} terminé — {len(plan)} endpoint(s), {total} settle(s) réussi(s) ===")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="soumet réellement le paiement on-chain")
    ap.add_argument("--only", default=None, help="un seul path, ex. /gleif/lei")
    ap.add_argument("--no-preflight", action="store_true")
    args = ap.parse_args()
    if not args.no_preflight:
        preflight()
    if args.execute:
        print(">>> MODE EXECUTE : settlement RÉEL on-chain <<<\n")
    return run(args.execute, args.only)


if __name__ == "__main__":
    sys.exit(main())
