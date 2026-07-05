"""Sonde read-only du CDP Bazaar discovery : nos ressources Solana sont-elles indexées ?

Interroge /discovery/search (mots-clés de nos endpoints) et /discovery/resources,
puis filtre par notre seller / domaine. Aucune écriture, aucun paiement.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx
from nacl.signing import SigningKey

FAC = os.environ["FACILITATOR_URL"].rstrip("/")
KID = os.environ["CDP_API_KEY_ID"]
SEC = os.environ["CDP_API_KEY_SECRET"]
SELLER = os.getenv("SOLANA_SELLER_ADDRESS", "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF")
DOMAIN = "x402-solana.onrender.com"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(method: str, path: str) -> str:
    host = urlsplit(FAC).netloc
    now = int(time.time())
    h = {"alg": "EdDSA", "kid": KID, "typ": "JWT", "nonce": secrets.token_hex(16)}
    cl = {"sub": KID, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120,
          "uris": [f"{method} {host}{urlsplit(FAC).path}{path}"]}
    si = f"{_b64url(json.dumps(h,separators=(',',':')).encode())}.{_b64url(json.dumps(cl,separators=(',',':')).encode())}"
    sig = SigningKey(base64.b64decode(SEC)[:32]).sign(si.encode()).signature
    return f"{si}.{_b64url(sig)}"


def _hits(items):
    out = []
    for it in items or []:
        res = (it.get("resource") or "") if isinstance(it, dict) else ""
        pay = json.dumps(it)
        if DOMAIN in res or SELLER in pay:
            out.append(res or it)
    return out


def main() -> None:
    with httpx.Client(timeout=25.0) as c:
        print("=== /discovery/search (per endpoint keyword) ===")
        for q in ["solana pre-trade", "solana token safety", "token dossier", "GLEIF LEI",
                  "sanctions screening", "polymarket odds", "x402 visibility audit"]:
            r = c.get(f"{FAC}/discovery/search", params={"query": q, "limit": 25, "offset": 0},
                      headers={"Authorization": f"Bearer {jwt('GET','/discovery/search')}"})
            data = r.json() if r.status_code == 200 else {}
            items = data.get("resources") or data.get("items") or []
            mine = _hits(items)
            print(f"  [{q}] http={r.status_code} total={len(items)} mine={len(mine)} {mine[:2]}")

        print("=== /discovery/resources (scan for our seller/domain) ===")
        total_mine = []
        for offset in range(0, 200, 50):
            r = c.get(f"{FAC}/discovery/resources", params={"limit": 50, "offset": offset},
                      headers={"Authorization": f"Bearer {jwt('GET','/discovery/resources')}"})
            if r.status_code != 200:
                print(f"  offset={offset} http={r.status_code}"); break
            items = r.json().get("resources") or r.json().get("items") or []
            total_mine += _hits(items)
            if len(items) < 50:
                break
        print(f"  our indexed resources: {len(total_mine)}")
        for m in total_mine:
            print("   -", m)


if __name__ == "__main__":
    main()
