"""Dry-run (aucun paiement) : vérifie /health (200) et les réponses 402 des 10
endpoints gated. Décode le header payment-required et contrôle que les `accepts`
sont bien Solana (network CAIP-2, mint USDC, payTo seller, montant atomique, feePayer).

À lancer contre un uvicorn déjà démarré (BASE_URL, défaut http://127.0.0.1:8000).
"""
from __future__ import annotations

import base64
import json
import os
import sys

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
SELLER = os.getenv("SOLANA_SELLER_ADDRESS", "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_NET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# endpoint path -> (query, expected price in USDC atomic units (6 decimals))
ENDPOINTS = [
    ("/gleif/lei", {"lei": "529900T8BM49AURSDO55"}, 10000),
    ("/sanctions/screen", {"name": "Saddam Hussein"}, 50000),
    ("/polymarket/odds", {"market": "2654605"}, 50000),
    ("/crypto/token-safety", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}, 50000),
    ("/crypto/pre-trade-verdict", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}, 50000),
    ("/crypto/token-dossier", {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}, 100000),
    ("/solana/token-safety", {"mint": USDC}, 10000),
    ("/solana/pre-trade", {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"}, 50000),
    ("/agent/rank-check", {"seller": "api.example.com"}, 100000),
    ("/agent/visibility-audit", {"seller": "api.example.com"}, 1000000),
]


def _decode_402(resp: httpx.Response) -> dict:
    # V2 : requirements complets dans le header (base64 JSON). Corps HTTP = {}.
    for hname in ("payment-required", "www-authenticate", "x-payment-required"):
        raw = resp.headers.get(hname)
        if raw:
            # certains SDK préfixent "x402 " ; on isole le base64
            token = raw.split()[-1] if " " in raw else raw
            try:
                return json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
            except Exception:
                pass
    # fallback : corps JSON
    try:
        body = resp.json()
        if body:
            return body
    except Exception:
        pass
    return {}


def main() -> int:
    ok = True
    with httpx.Client(timeout=30.0) as c:
        h = c.get(f"{BASE}/health")
        print(f"[health] HTTP {h.status_code} network={h.json().get('network')} pay_to={h.json().get('pay_to')}")
        if h.status_code != 200 or h.json().get("network") != SOLANA_NET:
            print("  !! health KO"); ok = False

        for path, query, expected_atomic in ENDPOINTS:
            r = c.get(f"{BASE}{path}", params=query)
            if r.status_code != 402:
                print(f"[{path}] !! attendu 402, reçu {r.status_code}"); ok = False; continue
            req = _decode_402(r)
            accepts = req.get("accepts") or []
            if not accepts:
                print(f"[{path}] !! pas d'accepts décodable (headers={list(r.headers)})"); ok = False; continue
            a = accepts[0]
            net = a.get("network"); pay_to = a.get("payTo") or a.get("pay_to")
            asset = a.get("asset"); amount = a.get("maxAmountRequired") or a.get("amount")
            extra = a.get("extra") or {}
            fee_payer = extra.get("feePayer")
            problems = []
            if net != SOLANA_NET: problems.append(f"network={net}")
            if pay_to != SELLER: problems.append(f"payTo={pay_to}")
            if asset != USDC: problems.append(f"asset={asset}")
            if str(amount) != str(expected_atomic): problems.append(f"amount={amount}!={expected_atomic}")
            if not fee_payer: problems.append("feePayer manquant")
            status = "OK" if not problems else "KO " + "; ".join(problems)
            print(f"[{path}] 402 amount={amount} net={net} asset={asset[:6]}.. payTo={str(pay_to)[:6]}.. feePayer={str(fee_payer)[:6] if fee_payer else None}.. -> {status}")
            if problems: ok = False
    print("\n=== DRY-RUN " + ("PASS ===" if ok else "FAIL ==="))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
