"""Mesure notre RANG dans la recherche du CDP Bazaar pour les requêtes Solana clés,
et compte combien de résultats Solana vs Base/EVM ressortent (contexte concurrentiel)."""
from __future__ import annotations
import base64, json, os, secrets, time
from urllib.parse import urlsplit
import httpx
from nacl.signing import SigningKey

FAC = os.environ["FACILITATOR_URL"].rstrip("/")
KID = os.environ["CDP_API_KEY_ID"]; SEC = os.environ["CDP_API_KEY_SECRET"]
SELLER = "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF"
DOMAIN = "x402-solana-cva8.onrender.com"

QUERIES = ["solana pre-trade", "solana token safety", "GLEIF LEI", "sanctions screening",
           "polymarket odds", "token dossier", "x402 visibility audit", "solana rug honeypot"]


def _b(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(path):
    host = urlsplit(FAC).netloc; base = urlsplit(FAC).path; now = int(time.time())
    h = {"alg": "EdDSA", "kid": KID, "typ": "JWT", "nonce": secrets.token_hex(16)}
    c = {"sub": KID, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120,
         "uris": [f"GET {host}{base}{path}"]}
    si = f"{_b(json.dumps(h, separators=(',', ':')).encode())}.{_b(json.dumps(c, separators=(',', ':')).encode())}"
    return f"{si}.{_b(SigningKey(base64.b64decode(SEC)[:32]).sign(si.encode()).signature)}"


def is_ours(it):
    blob = json.dumps(it)
    return DOMAIN in blob or SELLER in blob


def is_solana(it):
    acc = (it.get("accepts") or [{}])[0]
    return str(acc.get("network", "")).startswith("solana")


with httpx.Client(timeout=25.0) as cl:
    for q in QUERIES:
        r = cl.get(f"{FAC}/discovery/search", params={"query": q, "limit": 20},
                   headers={"Authorization": f"Bearer {jwt('/discovery/search')}"})
        if r.status_code != 200:
            print(f"[{q:26}] http={r.status_code}"); continue
        items = r.json().get("resources", []) or []
        our_ranks = [i + 1 for i, it in enumerate(items) if is_ours(it)]
        sol_total = sum(1 for it in items if is_solana(it))
        best = our_ranks[0] if our_ranks else None
        print(f"[{q:26}] total={len(items):2} solana={sol_total:2} nous={len(our_ranks):2} "
              f"best_rank={best if best else '-'} (ranks={our_ranks})")
