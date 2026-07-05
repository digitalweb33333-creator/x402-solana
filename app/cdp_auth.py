"""Authentification du facilitator CDP de production (x402 mainnet).

Le facilitator CDP (https://api.cdp.coinbase.com/platform/v2/x402) exige un Bearer
JWT signé avec la clé API CDP (Ed25519). On génère ce JWT par requête (court,
~120 s) via PyNaCl (déjà dépendance du SDK x402) — aucune dépendance supplémentaire.

On expose `make_cdp_create_headers(...)` qui renvoie un callable compatible avec
`x402.http.FacilitatorConfig` (CreateHeadersAuthProvider) : il produit les en-têtes
Authorization Bearer pour chaque opération (verify / settle / supported), chacune
signée avec son couple (méthode HTTP, URI) propre comme l'exige CDP.

Format JWT CDP (réf. docs.cdp.coinbase.com) :
- header : {"alg":"EdDSA","kid":<key_id>,"typ":"JWT","nonce":<hex>}
- claims : {"sub":<key_id>,"iss":"cdp","aud":["cdp_service"],"nbf":now,"exp":now+120,
            "uris":["<METHOD> <host><path>"]}  (host sans scheme, path inclus, sans query)
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from urllib.parse import urlsplit

from nacl.signing import SigningKey


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _signing_key(secret_b64: str) -> SigningKey:
    """La clé secrète CDP est un base64 de 64 octets (seed Ed25519 32o + clé publique 32o)."""
    raw = base64.b64decode(secret_b64)
    if len(raw) < 32:
        raise ValueError("CDP_API_KEY_SECRET invalide (Ed25519 attendu, ≥ 32 octets après base64).")
    return SigningKey(raw[:32])


def generate_cdp_jwt_for_uris(key_id: str, secret_b64: str, uris: list[str],
                              expires_in: int = 120) -> str:
    """Génère un Bearer JWT CDP (EdDSA/Ed25519) couvrant une ou plusieurs URI.

    Le claim `uris` est un tableau : un même JWT est accepté pour TOUTES les URI
    listées. C'est ce qui permet à un seul header `bazaar` de couvrir à la fois
    `GET …/discovery/resources` et `GET …/discovery/search` (chemins différents),
    là où le SDK n'expose qu'un seul slot `AuthHeaders.bazaar`.

    Chaque URI doit être déjà formatée `"<METHOD> <host><path>"` (host sans scheme).
    """
    sk = _signing_key(secret_b64)
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": key_id, "typ": "JWT", "nonce": secrets.token_hex(16)}
    claims = {
        "sub": key_id,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + expires_in,
        "uris": list(uris),
    }
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = sk.sign(signing_input.encode("ascii")).signature
    return f"{signing_input}.{_b64url(signature)}"


def generate_cdp_jwt(key_id: str, secret_b64: str, method: str, host: str, path: str,
                     expires_in: int = 120) -> str:
    """Génère un Bearer JWT CDP (EdDSA/Ed25519) pour une requête (method + host + path)."""
    return generate_cdp_jwt_for_uris(
        key_id, secret_b64, [f"{method.upper()} {host}{path}"], expires_in
    )


def make_cdp_create_headers(facilitator_url: str, key_id: str, secret_b64: str):
    """Construit le callable `create_headers` attendu par FacilitatorConfig (CDP).

    Renvoie, à chaque appel, des JWT frais pour verify/settle/supported, chacun signé
    avec sa (méthode, URI) propre. Les chemins dérivent de l'URL du facilitator.
    """
    parts = urlsplit(facilitator_url)
    host = parts.netloc
    base_path = parts.path.rstrip("/")  # ex. /platform/v2/x402

    def _bearer(method: str, sub_path: str) -> dict[str, str]:
        jwt = generate_cdp_jwt(key_id, secret_b64, method, host, f"{base_path}{sub_path}")
        return {"Authorization": f"Bearer {jwt}"}

    def _bearer_multi(uri_pairs: list[tuple[str, str]]) -> dict[str, str]:
        uris = [f"{m.upper()} {host}{base_path}{p}" for m, p in uri_pairs]
        jwt = generate_cdp_jwt_for_uris(key_id, secret_b64, uris)
        return {"Authorization": f"Bearer {jwt}"}

    def create_headers() -> dict[str, dict[str, str]]:
        return {
            "verify": _bearer("POST", "/verify"),
            "settle": _bearer("POST", "/settle"),
            "supported": _bearer("GET", "/supported"),
            "list": _bearer("GET", "/supported"),
            # Discovery Bazaar : un seul slot `AuthHeaders.bazaar` côté SDK, mais deux
            # chemins (list + search) → JWT multi-URI couvrant les deux.
            "bazaar": _bearer_multi([
                ("GET", "/discovery/resources"),
                ("GET", "/discovery/search"),
            ]),
        }

    return create_headers
