"""Enregistre les 10 endpoints sur 402index.io (annuaire multi-protocole, sans compte).

POST https://402index.io/api/v1/register (idempotent par URL). 402index PROBE chaque
endpoint en live (doit renvoyer un 402 x402 valide) → À LANCER APRÈS le déploiement Render,
quand https://x402-solana.onrender.com est joignable.

Usage :  BASE_URL=https://x402-solana.onrender.com ./.venv/bin/python tools/register_402index.py
         (ajouter --verify pour lister ce que 402index connaît de nous)
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("BASE_URL", "https://x402-solana.onrender.com").rstrip("/")
REGISTER = "https://402index.io/api/v1/register"

PATHS = [
    "/gleif/lei", "/sanctions/screen", "/polymarket/odds", "/crypto/token-safety",
    "/crypto/pre-trade-verdict", "/crypto/token-dossier", "/solana/token-safety",
    "/solana/pre-trade", "/agent/rank-check", "/agent/visibility-audit",
]


def main() -> int:
    verify = "--verify" in sys.argv
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        if verify:
            r = c.get("https://402index.io/api/search", params={"q": "x402-solana"})
            print("verify:", r.status_code, r.text[:800])
            return 0
        ok = 0
        for p in PATHS:
            url = f"{BASE}{p}"
            try:
                r = c.post(REGISTER, json={"url": url})
                status = r.status_code
                body = r.text[:160].replace("\n", " ")
                print(f"[{p}] {status} {body}")
                if status in (200, 201):
                    ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{p}] ERROR {type(e).__name__}: {e}")
        print(f"\n=== 402index: {ok}/{len(PATHS)} enregistrés ===")
        return 0 if ok == len(PATHS) else 1


if __name__ == "__main__":
    sys.exit(main())
