"""Helpers Solana — RPC public keyless + DexScreener/GeckoTerminal pour la liquidité.

Sources GRATUITES, sans clé :
- RPC mainnet public (publicnode + api.mainnet-beta en failover) — getAccountInfo
  (mint jsonParsed : mint/freeze authority, supply, decimals), getTokenLargestAccounts
  (concentration des holders), getSignaturesForAddress (vélocité / activité récente).
- DexScreener (api.dexscreener.com) — liquidité, prix, volume, âge du pool (pairCreatedAt).

Tout est best-effort : chaque helper renvoie (data, None) ou (None, "erreur") et ne
lève jamais. Le routeur décide d'ABSTAIN si les données sont insuffisantes.
"""
from __future__ import annotations

import re
from typing import Any

from app.sources.http_util import client, get_json, post_json

# Alphabet base58 Bitcoin/Solana (sans 0 O I l).
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _b58decode(s: str) -> bytes | None:
    """Décode base58 en pur Python (zéro dépendance). None si caractère invalide."""
    num = 0
    for ch in s:
        v = _B58_INDEX.get(ch)
        if v is None:
            return None
        num = num * 58 + v
    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full

# RPC publics keyless, essayés dans l'ordre (failover si rate-limit/erreur).
SOLANA_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]
# base58, 32-44 chars (adresse Solana / mint SPL)
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Whitelist anti-faux-positif : majors qu'aucun check ne doit flagger comme rug.
# (mint authority souvent non-null sur des actifs natifs/bridgés parfaitement sûrs.)
KNOWN_SAFE = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "Wrapped SOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "Marinade staked SOL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "Ether (Wormhole)",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "Jupiter",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "Bonk",
}


def valid_mint(mint: str) -> bool:
    """Adresse Solana = 32 octets encodés base58 (32-44 chars)."""
    m = (mint or "").strip()
    if not MINT_RE.match(m):
        return False
    decoded = _b58decode(m)
    return decoded is not None and len(decoded) == 32


# RPCs acceptant getTokenLargestAccounts (publicnode le BLOQUE) — mainnet-beta d'abord.
HOLDER_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]


async def rpc(method: str, params: list[Any], rpcs: list[str] | None = None) -> tuple[Any, str | None]:
    """Appel JSON-RPC Solana avec failover entre RPC publics. (result, None)|(None, err).

    Un échec JSON-RPC sur un RPC (méthode bloquée, rate-limit) N'INTERROMPT PAS la
    boucle : on bascule sur le RPC suivant. Erreur renvoyée seulement si TOUS échouent.
    """
    c = await client("solana", timeout=12.0)
    last = "unknown"
    for url in (rpcs or SOLANA_RPCS):
        data, err = await post_json(
            c, url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, attempts=1
        )
        if err:
            last = err
            continue
        if isinstance(data, dict) and "result" in data:
            return data["result"], None
        if isinstance(data, dict) and data.get("error"):
            last = str(data["error"].get("message", "rpc_error"))
            continue  # méthode bloquée / rate-limit sur ce RPC -> essayer le suivant
        last = "no_result"
    return None, last


async def get_mint_info(mint: str) -> tuple[dict | None, str | None]:
    """Parse le compte Mint SPL : mint/freeze authority, supply, decimals, initialized."""
    res, err = await rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    if err:
        return None, err
    value = (res or {}).get("value")
    if not value:
        return None, "mint_not_found"
    parsed = (((value.get("data") or {}).get("parsed")) or {})
    info = parsed.get("info") or {}
    owner_program = value.get("owner")  # SPL Token program owns mints
    if parsed.get("type") != "mint":
        return None, "not_a_mint"
    return {
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
        "decimals": info.get("decimals"),
        "supply": info.get("supply"),
        "is_initialized": info.get("isInitialized"),
        "owner_program": owner_program,
        "is_token_2022": owner_program == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }, None


async def get_largest_accounts(mint: str) -> tuple[list | None, str | None]:
    """Top ~20 comptes détenteurs (concentration). Montants bruts.

    getTokenLargestAccounts est throttlé/bloqué sur la plupart des RPC keyless :
    on cible HOLDER_RPCS (mainnet-beta d'abord, publicnode le bloque) et on tolère
    l'échec — le routeur dégrade alors proprement le module concentration en 'limited'.
    """
    res, err = await rpc("getTokenLargestAccounts", [mint], rpcs=HOLDER_RPCS)
    if err:
        return None, err
    return (res or {}).get("value") or [], None


async def get_recent_signatures(address: str, limit: int = 100) -> tuple[list | None, str | None]:
    """Signatures récentes (vélocité / activité). blockTime en epoch s."""
    res, err = await rpc("getSignaturesForAddress", [address, {"limit": max(1, min(limit, 1000))}])
    if err:
        return None, err
    return res or [], None


async def dexscreener_token(mint: str) -> tuple[dict | None, str | None]:
    """Meilleure paire (max liquidité USD) pour ce mint sur DexScreener (Solana)."""
    c = await client("dexscreener", timeout=12.0)
    data, err = await get_json(c, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if err:
        return None, err
    pairs = [p for p in ((data or {}).get("pairs") or []) if (p.get("chainId") == "solana")]
    if not pairs:
        return None, "no_pairs"
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
    return best, None


async def solana_first_seen(addr: str) -> str | None:
    """Borne INFÉRIEURE d'âge : blockTime de la plus ancienne signature accessible (≤1000)."""
    import time as _t

    sigs, err = await get_recent_signatures(addr, limit=1000)
    if err or not sigs:
        return None
    times = [s.get("blockTime") for s in sigs if s.get("blockTime")]
    if not times:
        return None
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(min(times)))


def num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
