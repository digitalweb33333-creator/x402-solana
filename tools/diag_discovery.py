"""Diagnostic brut de l'API discovery du facilitator CDP (status + corps)."""
from __future__ import annotations
import base64, json, os, secrets, time
from urllib.parse import urlsplit
import httpx
from nacl.signing import SigningKey

FAC = os.environ["FACILITATOR_URL"].rstrip("/")
KID = os.environ["CDP_API_KEY_ID"]; SEC = os.environ["CDP_API_KEY_SECRET"]

def _b(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt(method, path):
    host=urlsplit(FAC).netloc; base=urlsplit(FAC).path; now=int(time.time())
    h={"alg":"EdDSA","kid":KID,"typ":"JWT","nonce":secrets.token_hex(16)}
    c={"sub":KID,"iss":"cdp","aud":["cdp_service"],"nbf":now,"exp":now+120,"uris":[f"{method} {host}{base}{path}"]}
    si=f"{_b(json.dumps(h,separators=(',',':')).encode())}.{_b(json.dumps(c,separators=(',',':')).encode())}"
    return f"{si}.{_b(SigningKey(base64.b64decode(SEC)[:32]).sign(si.encode()).signature)}"

with httpx.Client(timeout=25.0) as cl:
    for path, params in [
        ("/discovery/resources", {"limit": 5, "offset": 0}),
        ("/discovery/resources", None),
        ("/discovery/search", {"query": "solana", "limit": 5, "offset": 0}),
        ("/discovery/search", {"q": "solana", "limit": 5}),
    ]:
        try:
            r = cl.get(f"{FAC}{path}", params=params, headers={"Authorization": f"Bearer {jwt('GET', path)}"})
            body = r.text[:400].replace("\n"," ")
            print(f"GET {path} params={params} -> {r.status_code} {body}")
        except Exception as e:
            print(f"GET {path} ERROR {e}")
