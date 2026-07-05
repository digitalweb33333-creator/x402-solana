"""Sonde read-only du CDP Bazaar : nos ressources Solana sont-elles indexées ?

Appel direct /discovery/search + /discovery/resources (GET + JWT CDP, SANS header
Content-Type — le facilitator renvoie 400 sinon). Filtre les hits par réseau Solana
+ notre seller/domaine. Aucune écriture, aucun paiement. 0 = en attente d'indexation
(le Bazaar crawle périodiquement, plusieurs heures).
"""
from __future__ import annotations
import base64, json, os, secrets, time
from urllib.parse import urlsplit
import httpx
from nacl.signing import SigningKey

FAC = os.environ["FACILITATOR_URL"].rstrip("/")
KID = os.environ["CDP_API_KEY_ID"]; SEC = os.environ["CDP_API_KEY_SECRET"]
SELLER = "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF"
DOMAIN = "x402-solana-cva8.onrender.com"
QUERIES = ["solana pre-trade", "solana token safety", "token dossier", "GLEIF LEI",
           "sanctions screening", "polymarket odds", "x402 visibility audit"]

def _b(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt(path):
    host=urlsplit(FAC).netloc; base=urlsplit(FAC).path; now=int(time.time())
    h={"alg":"EdDSA","kid":KID,"typ":"JWT","nonce":secrets.token_hex(16)}
    c={"sub":KID,"iss":"cdp","aud":["cdp_service"],"nbf":now,"exp":now+120,"uris":[f"GET {host}{base}{path}"]}
    si=f"{_b(json.dumps(h,separators=(',',':')).encode())}.{_b(json.dumps(c,separators=(',',':')).encode())}"
    return f"{si}.{_b(SigningKey(base64.b64decode(SEC)[:32]).sign(si.encode()).signature)}"

def _mine(items):
    out=[]
    for it in items or []:
        blob=json.dumps(it)
        acc=(it.get("accepts") or [{}])[0]
        net=str(acc.get("network","")); pay=acc.get("payTo","")
        res=it.get("resource") or acc.get("resource") or ""
        res=res.get("url") if isinstance(res,dict) else res
        if DOMAIN in blob or (net.startswith("solana") and pay==SELLER):
            out.append(res or f"{net}|{pay}")
    return out

def main():
    with httpx.Client(timeout=25.0) as cl:
        found=set()
        print("=== /discovery/search (Solana hits only) ===")
        for q in QUERIES:
            r=cl.get(f"{FAC}/discovery/search", params={"query":q,"limit":20,"offset":0},
                     headers={"Authorization":f"Bearer {jwt('/discovery/search')}"})
            items=(r.json().get("resources") if r.status_code==200 else []) or []
            mine=_mine(items); found.update(mine)
            print(f"  [{q:24}] http={r.status_code} total={len(items)} solana-mine={len(mine)}")
        print("=== /discovery/resources full scan ===")
        for off in range(0,600,20):
            r=cl.get(f"{FAC}/discovery/resources", params={"limit":20,"offset":off},
                     headers={"Authorization":f"Bearer {jwt('/discovery/resources')}"})
            if r.status_code!=200: print(f"  offset={off} http={r.status_code} {r.text[:120]}"); break
            items=r.json().get("items") or r.json().get("resources") or []
            found.update(_mine(items))
            if len(items)<20: break
        print(f"\n=== indexé (Solana, notre seller/domaine): {len(found)} ===")
        for u in sorted(found): print("   -", u)
        if not found: print("   (0 → en attente d'indexation ; crawl périodique du Bazaar)")

if __name__=="__main__":
    main()
