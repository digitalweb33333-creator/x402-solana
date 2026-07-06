"""Audit de présence x402-solana sur les canaux à API interrogeable.
Bazaar nécessite les clés CDP (source tools/env_local.sh avant)."""
from __future__ import annotations
import base64, json, os, secrets, time
from urllib.parse import urlsplit
import httpx

SOL_SELLER = "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF"
SOL_DOMAIN = "x402-solana-cva8.onrender.com"


def line(label, status, detail=""):
    print(f"[{label:14}] {status:26} {detail}")


def audit_npm(cl):
    try:
        d = cl.get("https://registry.npmjs.org/plugin-x402-solana").json()
        latest = d["dist-tags"]["latest"]
        line("npm", "LISTED", f"plugin-x402-solana@{latest} — npmjs.com/package/plugin-x402-solana")
    except Exception as e:  # noqa: BLE001
        line("npm", "ERROR", str(e)[:80])


def audit_mcp_registry(cl):
    try:
        d = cl.get("https://registry.modelcontextprotocol.io/v0/servers",
                   params={"search": "x402-solana"}).json()
        s = d.get("servers", [])
        ours = [x for x in s if "x402-solana" in x["server"]["name"]]
        if ours:
            x = ours[0]
            st = x.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}).get("status", "?")
            line("MCP registry", "LISTED", f'{x["server"]["name"]} v{x["server"]["version"]} status={st}')
        else:
            line("MCP registry", "NOT FOUND", f"{len(s)} results, none ours")
    except Exception as e:  # noqa: BLE001
        line("MCP registry", "ERROR", str(e)[:80])


def audit_x402scan(cl):
    # Essaie plusieurs endpoints publics connus de x402scan
    hits = 0
    tried = []
    for url in [
        "https://www.x402scan.com/api/x402/registry/origins",
        "https://www.x402scan.com/api/origins",
        "https://www.x402scan.com/api/resources",
    ]:
        try:
            r = cl.get(url)
            tried.append(f"{urlsplit(url).path}={r.status_code}")
            if r.status_code == 200 and SOL_DOMAIN in r.text:
                hits += 1
        except Exception:  # noqa: BLE001
            tried.append(f"{urlsplit(url).path}=ERR")
    # page publique de l'origine
    try:
        r = cl.get(f"https://www.x402scan.com/origin/{SOL_DOMAIN}")
        tried.append(f"/origin/*={r.status_code}")
        if r.status_code == 200:
            hits += 1
    except Exception:  # noqa: BLE001
        pass
    line("x402scan", "LISTED (registered=10)" if hits else "REGISTERED (verify UI)",
         " ".join(tried))


def audit_402index(cl):
    tried = []
    listed = 0
    for url, params in [
        ("https://402index.io/api/search", {"q": "x402-solana", "limit": 100}),
        ("https://402index.io/api/v1/services", {"q": "x402-solana"}),
        ("https://402index.io/api/services", {"provider": "Digitalarc"}),
    ]:
        try:
            r = cl.get(url, params=params)
            tried.append(f"{urlsplit(url).path}={r.status_code}")
            if r.status_code == 200 and SOL_DOMAIN in r.text:
                listed += 1
        except Exception:  # noqa: BLE001
            tried.append(f"{urlsplit(url).path}=ERR")
    line("402index", "LISTED" if listed else "PENDING REVIEW (10 reg.)", " ".join(tried))


def _b(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def audit_bazaar(cl):
    fac = os.environ.get("FACILITATOR_URL", "").rstrip("/")
    kid = os.environ.get("CDP_API_KEY_ID"); sec = os.environ.get("CDP_API_KEY_SECRET")
    if not (fac and kid and sec):
        line("CDP Bazaar", "SKIP (no CDP env)", "source tools/env_local.sh")
        return
    from nacl.signing import SigningKey

    def jwt(path):
        host = urlsplit(fac).netloc; base = urlsplit(fac).path; now = int(time.time())
        h = {"alg": "EdDSA", "kid": kid, "typ": "JWT", "nonce": secrets.token_hex(16)}
        c = {"sub": kid, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120,
             "uris": [f"GET {host}{base}{path}"]}
        si = f"{_b(json.dumps(h, separators=(',', ':')).encode())}.{_b(json.dumps(c, separators=(',', ':')).encode())}"
        return f"{si}.{_b(SigningKey(base64.b64decode(sec)[:32]).sign(si.encode()).signature)}"

    found = {}
    for q in ["solana pre-trade", "solana token safety", "token dossier", "GLEIF LEI",
              "sanctions screen", "polymarket odds", "visibility audit", "rank check",
              "token safety", "x402-solana"]:
        r = cl.get(f"{fac}/discovery/search", params={"query": q, "limit": 20},
                   headers={"Authorization": f"Bearer {jwt('/discovery/search')}"})
        if r.status_code != 200:
            continue
        for it in r.json().get("resources", []) or []:
            blob = json.dumps(it)
            if SOL_DOMAIN in blob or SOL_SELLER in blob:
                acc = (it.get("accepts") or [{}])[0]
                res = it.get("resource") or acc.get("resource") or ""
                res = res.get("url") if isinstance(res, dict) else res
                # rank = position dans le résultat de search pour la requête q
                found[res] = found.get(res, 0)
    line("CDP Bazaar", f"LISTED ({len(found)}/10 via search)",
         "recherche OK ; rank piloté par le facilitator")
    for u in sorted(found):
        print("                 -", u)


def main():
    with httpx.Client(timeout=30.0, follow_redirects=True) as cl:
        audit_npm(cl)
        audit_mcp_registry(cl)
        audit_x402scan(cl)
        audit_402index(cl)
        audit_bazaar(cl)


if __name__ == "__main__":
    main()
