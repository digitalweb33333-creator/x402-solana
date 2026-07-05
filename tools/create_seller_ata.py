"""Crée l'ATA USDC du seller Solana (prérequis au 1er settlement).

Le client x402 SVM construit un TransferChecked SANS instruction create-ATA :
il suppose que l'ATA destinataire (seller) existe. Si absent, le 1er settle
échoue. Ce script crée l'ATA USDC du seller de façon IDEMPOTENTE, rent payé par
le wallet buyer (qui a du SOL). Transaction on-chain unique (~0.00204 SOL).

DRY par défaut (n'envoie rien). --execute pour soumettre (APRÈS le "go").

Usage :
  ./.venv/bin/python -m tools.create_seller_ata            # dry (affiche le plan)
  ./.venv/bin/python -m tools.create_seller_ata --execute  # crée réellement l'ATA
"""
from __future__ import annotations

import argparse
import os
import sys

from app.config import SOLANA_RPC_URL, SOLANA_SELLER_ADDRESS, SOLANA_USDC_MINT

BUYER_KEY = os.environ["SOLANA_BUYER_PRIVATE_KEY"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    from solana.rpc.api import Client
    from solana.rpc.commitment import Confirmed
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from spl.token.instructions import (
        create_idempotent_associated_token_account,
        get_associated_token_address,
    )

    payer = Keypair.from_base58_string(BUYER_KEY)
    seller = Pubkey.from_string(SOLANA_SELLER_ADDRESS)
    mint = Pubkey.from_string(SOLANA_USDC_MINT)
    ata = get_associated_token_address(seller, mint)

    c = Client(SOLANA_RPC_URL)
    exists = c.get_account_info(ata).value is not None
    print(f"seller        : {SOLANA_SELLER_ADDRESS}")
    print(f"USDC mint     : {SOLANA_USDC_MINT}")
    print(f"seller USDC ATA: {ata}")
    print(f"already exists : {exists}")
    print(f"rent payer     : {payer.pubkey()} (buyer)")
    if exists:
        print("ATA déjà présent — rien à faire.")
        return 0
    if not args.execute:
        print("\nDRY: création NON soumise. Relancer avec --execute pour créer (rent ~0.00204 SOL).")
        return 0

    ix = create_idempotent_associated_token_account(
        payer=payer.pubkey(), owner=seller, mint=mint
    )
    bh = c.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], bh)
    tx = VersionedTransaction(msg, [payer])
    sig = c.send_transaction(tx).value
    print(f"\nEXEC: tx envoyée -> {sig}")
    c.confirm_transaction(sig, commitment=Confirmed)
    after = c.get_account_info(ata).value is not None
    print(f"confirmé. ATA existe maintenant: {after}")
    return 0 if after else 1


if __name__ == "__main__":
    sys.exit(main())
