"""Listes de sanctions officielles publiques (gratuites) — multi-chain.

Source : 0xB10C/ofac-sanctioned-digital-currency-addresses (miroir machine-readable
des adresses crypto de l'OFAC SDN, mis à jour à chaque publication OFAC). Fichiers
par chaîne (ETH, BTC, XBT, SOL, TRX, XRP, LTC, BCH, ETC, ZEC, DASH, USDT, USDC…).

L'OFAC SDN est la SEULE source officielle qui publie des ADRESSES crypto. UN et
UK FCDO publient des noms/entités, pas des wallets ; on les déclare dans
`lists_checked` avec coverage="names_only" pour rester honnête (pas de faux PASS).

On expose aussi les adresses de mixers connus (Tornado Cash, sanctionnées OFAC) pour
l'analyse d'exposition. Tout est caché 24 h ; les helpers ne lèvent jamais.
"""
from __future__ import annotations

import re
from typing import Any

from app.sources.http_util import TTLCache, client, get_json

_RAW = ("https://raw.githubusercontent.com/0xB10C/"
        "ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_{}.json")

# Fichiers par symbole de chaîne. EVM = adresses 0x (comparées en minuscules).
_EVM_FILES = ["ETH", "USDC", "USDT", "ARB"]
_OTHER_FILES = ["BTC", "XBT", "SOL", "TRX", "XRP", "LTC", "BCH", "ETC", "ZEC", "DASH"]

_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache = TTLCache(86400)  # 24 h

# Mixers sanctionnés OFAC (Tornado Cash) — pour l'exposition 1-hop côté EVM.
TORNADO_CASH = {
    "0x8589427373d6d84e98730d7795d8f6f8731fda16",  # Tornado.Cash Router
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xdd4c48c0b24039969fc16d1cdf626eab821d3384",
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0xd96f2b1c14db8458374d9aca76e26c3d18364307",  # 1 ETH pool
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",  # 10 ETH pool
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # 100 ETH pool
    "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc",  # 0.1 ETH pool
    "0x07687e702b410fa43f4cb4af7fa097918ffd2730",  # 90 TRX (router-adjacent)
}
TORNADO_CASH = {a.lower() for a in TORNADO_CASH if _EVM_RE.match(a)}


async def _load_file(symbol: str) -> list[str]:
    c = await client("github", timeout=12.0)
    data, _ = await get_json(c, _RAW.format(symbol))
    return data if isinstance(data, list) else []


async def sanctioned_sets() -> tuple[set[str], set[str], dict[str, bool]]:
    """(evm_set_lowercased, other_set_exact, files_loaded). Caché 24 h."""
    cached = _cache.get("sets")
    if cached is not None:
        return cached
    evm: set[str] = set()
    other: set[str] = set()
    loaded: dict[str, bool] = {}
    for sym in _EVM_FILES:
        lst = await _load_file(sym)
        loaded[sym] = bool(lst)
        evm |= {a.lower() for a in lst if isinstance(a, str)}
    for sym in _OTHER_FILES:
        lst = await _load_file(sym)
        loaded[sym] = bool(lst)
        other |= {a for a in lst if isinstance(a, str)}
    result = (evm, other, loaded)
    if evm or other:
        _cache.set("sets", result)
    return result


async def ofac_evm_set() -> set[str]:
    """Juste le set EVM (réutilisé par le payment-firewall)."""
    evm, _, _ = await sanctioned_sets()
    return evm


def chain_of(addr: str) -> str:
    a = (addr or "").strip()
    if _EVM_RE.match(a):
        return "evm"
    if re.match(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{20,60}$", a):
        return "bitcoin"
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", a):
        return "solana"
    if re.match(r"^T[1-9A-HJ-NP-Za-km-z]{33}$", a):
        return "tron"
    if re.match(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$", a):
        return "xrp"
    return "unknown"


async def direct_screen(addr: str) -> dict[str, Any]:
    """Le wallet est-il LUI-MÊME sur une liste de sanctions ? (match direct, déterministe)."""
    evm, other, loaded = await sanctioned_sets()
    a = (addr or "").strip()
    chain = chain_of(a)
    if chain == "evm":
        hit = a.lower() in evm
        is_mixer = a.lower() in TORNADO_CASH
    else:
        hit = a in other
        is_mixer = False
    return {
        "listed": hit,
        "is_known_mixer": is_mixer,
        "detected_chain": chain,
        "lists_loaded": loaded,
        "evm_list_size": len(evm),
        "other_list_size": len(other),
    }
