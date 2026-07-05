"""Enregistre les 10 endpoints sur 402index.io (annuaire multi-protocole, sans compte).

POST https://402index.io/api/v1/register (idempotent par URL). Payload complet requis :
url, name, protocol, + métadonnées (description, price, network Solana, category).
402index PROBE chaque endpoint en live → à lancer contre l'URL live.

Usage :  BASE_URL=https://x402-solana-cva8.onrender.com ./.venv/bin/python tools/register_402index.py
         (--verify pour lister ce que 402index connaît de nous)
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("BASE_URL", "https://x402-solana-cva8.onrender.com").rstrip("/")
REGISTER = "https://402index.io/api/v1/register"
LIST = "https://402index.io/api/search"
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def _rows(c: httpx.Client) -> list[dict]:
    x = c.get(f"{BASE}/.well-known/x402.json").json()
    rows = []
    for r in x["resources"]:
        price = float(str(r.get("price", "0")).lstrip("$") or 0)
        rows.append({
            "url": f"{BASE}{r['resource']}",
            "name": f"{r.get('serviceName') or r['resource']} (x402-solana)"[:60],
            "protocol": "x402",
            "http_method": r.get("method", "GET"),
            "description": r.get("description", ""),
            "price_usd": price,
            "payment_asset": "USDC",
            "payment_network": NETWORK,
            "category": (r.get("tags") or ["data"])[0],
            "provider": "Digitalarc",
        })
    return rows


def main() -> int:
    with httpx.Client(timeout=40.0, follow_redirects=True) as c:
        if "--verify" in sys.argv:
            r = c.get(LIST, params={"q": "x402-solana", "limit": 100})
            print("verify:", r.status_code)
            try:
                data = r.json()
                svcs = data.get("services") or data.get("results") or data.get("items") or []
                ours = [s for s in svcs if "x402-solana-cva8" in str(s.get("url", ""))]
                print(f"  402index knows {len(ours)} of our endpoints:")
                for s in ours:
                    print(f"   - {s.get('name')} | {s.get('url')} | health={s.get('health_status')}")
            except Exception as e:  # noqa: BLE001
                print("  parse error:", e, r.text[:300])
            return 0
        ok = 0
        rows = _rows(c)
        for body in rows:
            try:
                r = c.post(REGISTER, json=body)
                detail = r.text[:140].replace("\n", " ")
                print(f"[{body['url'].split(BASE)[-1]}] {r.status_code} {detail}")
                if r.status_code in (200, 201):
                    ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{body['url']}] ERROR {type(e).__name__}: {e}")
        print(f"\n=== 402index: {ok}/{len(rows)} enregistrés ===")
        return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
