"""Read-only probe: does the CDP x402 facilitator advertise Solana support?

Calls GET <FACILITATOR_URL>/supported with a CDP Bearer JWT and prints every
supported (scheme, network) kind. No payment, no settlement — pure discovery.

Run with the Base venv python (has PyNaCl + httpx). Credentials are read from
the environment (never hardcoded).
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

FACILITATOR_URL = os.environ["FACILITATOR_URL"].strip()
KEY_ID = os.environ["CDP_API_KEY_ID"].strip()
SECRET = os.environ["CDP_API_KEY_SECRET"].strip()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_for(method: str, host: str, path: str) -> str:
    raw = base64.b64decode(SECRET)
    sk = SigningKey(raw[:32])
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": KEY_ID, "typ": "JWT", "nonce": secrets.token_hex(16)}
    claims = {
        "sub": KEY_ID, "iss": "cdp", "aud": ["cdp_service"],
        "nbf": now, "exp": now + 120, "uris": [f"{method.upper()} {host}{path}"],
    }
    si = f"{_b64url(json.dumps(header, separators=(',',':')).encode())}." \
         f"{_b64url(json.dumps(claims, separators=(',',':')).encode())}"
    sig = sk.sign(si.encode("ascii")).signature
    return f"{si}.{_b64url(sig)}"


def main() -> None:
    parts = urlsplit(FACILITATOR_URL)
    host, base_path = parts.netloc, parts.path.rstrip("/")
    token = jwt_for("GET", host, f"{base_path}/supported")
    r = httpx.get(f"{FACILITATOR_URL}/supported",
                  headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
    print("HTTP", r.status_code)
    data = r.json()
    kinds = data.get("kinds", data if isinstance(data, list) else [])
    print(f"total kinds: {len(kinds)}")
    solana = []
    for k in kinds:
        net = k.get("network", "")
        sch = k.get("scheme", "")
        extra = k.get("extra", {}) or {}
        fp = extra.get("feePayer")
        line = f"  scheme={sch!r:10} network={net!r}"
        if fp:
            line += f"  feePayer={fp}"
        print(line)
        if "solana" in str(net).lower():
            solana.append(k)
    print("\n=== SOLANA SUPPORT ===")
    if solana:
        print(f"YES — {len(solana)} Solana kind(s):")
        print(json.dumps(solana, indent=2))
    else:
        print("NO Solana kind advertised by this facilitator.")


if __name__ == "__main__":
    main()
