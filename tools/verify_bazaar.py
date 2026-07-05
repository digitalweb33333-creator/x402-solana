"""Vérifie l'indexation Bazaar : calibre sur Base (payTo EVM) pour confirmer le
mécanisme pay->index, puis cherche nos ressources Solana. Lecture seule."""
from __future__ import annotations
import base64, json, os, secrets, time
from urllib.parse import urlsplit
import httpx
from nacl.signing import SigningKey

FAC = os.environ["FACILITATOR_URL"].rstrip("/")
KID = os.environ["CDP_API_KEY_ID"]; SEC = os.environ["CDP_API_KEY_SECRET"]

SOL_SELLER = "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF"
SOL_DOMAIN = "x402-solana-cva8.onrender.com"
BASE_SELLER = "0x1D1B81247C407521E2A01F3E21514870dcf1620f"
BASE_DOMAIN = "x402-endpoints.onrender.com"


def _b(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(path: str) -> str:
    host = urlsplit(FAC).netloc; base = urlsplit(FAC).path; now = int(time.time())
    h = {"alg": "EdDSA", "kid": KID, "typ": "JWT", "nonce": secrets.token_hex(16)}
    c = {"sub": KID, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120,
         "uris": [f"GET {host}{base}{path}"]}
    si = f"{_b(json.dumps(h, separators=(',', ':')).encode())}.{_b(json.dumps(c, separators=(',', ':')).encode())}"
    return f"{si}.{_b(SigningKey(base64.b64decode(SEC)[:32]).sign(si.encode()).signature)}"


def scan_all(cl: httpx.Client) -> list[dict]:
    """Full /discovery/resources scan (limit<=20)."""
    out = []
    for off in range(0, 2000, 20):
        r = cl.get(f"{FAC}/discovery/resources", params={"limit": 20, "offset": off},
                   headers={"Authorization": f"Bearer {jwt('/discovery/resources')}"})
        if r.status_code != 200:
            print(f"  scan stop offset={off} http={r.status_code}"); break
        items = r.json().get("items") or r.json().get("resources") or []
        out.extend(items)
        if len(items) < 20:
            break
    return out


def match(items, seller, domain):
    hits = []
    for it in items:
        blob = json.dumps(it)
        acc = (it.get("accepts") or [{}])[0]
        pay = acc.get("payTo", "")
        res = it.get("resource") or acc.get("resource") or ""
        res = res.get("url") if isinstance(res, dict) else res
        if seller.lower() in blob.lower() or domain in blob:
            hits.append(res or f"{acc.get('network')}|{pay}")
    return hits


def main() -> None:
    with httpx.Client(timeout=25.0) as cl:
        items = scan_all(cl)
        print(f"Bazaar /discovery/resources : {len(items)} ressources totales")
        base_hits = match(items, BASE_SELLER, BASE_DOMAIN)
        sol_hits = match(items, SOL_SELLER, SOL_DOMAIN)
        print(f"  Base (x402-endpoints) indexé : {len(base_hits)}")
        for u in sorted(set(base_hits)): print("     -", u)
        print(f"  Solana (x402-solana) indexé  : {len(sol_hits)}")
        for u in sorted(set(sol_hits)): print("     -", u)

        # aussi via search (au cas où resources full-list ne pagine pas tout)
        print("\n=== search fallback ===")
        seen_sol, seen_base = set(), set()
        for q in ["solana", "x402-solana", "pre-trade", "token safety", "GLEIF", "sanctions",
                  "polymarket", "visibility audit", "rank check", "token dossier"]:
            r = cl.get(f"{FAC}/discovery/search", params={"query": q, "limit": 20},
                       headers={"Authorization": f"Bearer {jwt('/discovery/search')}"})
            if r.status_code != 200:
                print(f"  [{q}] http={r.status_code}"); continue
            its = r.json().get("resources") or []
            seen_sol.update(match(its, SOL_SELLER, SOL_DOMAIN))
            seen_base.update(match(its, BASE_SELLER, BASE_DOMAIN))
        print(f"  search: Base hits={len(seen_base)} | Solana hits={len(seen_sol)}")
        for u in sorted(seen_sol): print("     SOL -", u)


if __name__ == "__main__":
    main()
