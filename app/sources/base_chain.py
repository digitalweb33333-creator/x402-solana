"""Graphe de règlement on-chain (Base) — historique des paiements x402 reçus.

Un endpoint x402 est payé en USDC (EIP-3009 transferWithAuthorization) ; on-chain
cela apparaît comme un Transfer ERC-20 standard VERS le wallet vendeur. En lisant
les transferts USDC entrants d'un wallet via Blockscout (keyless, public), on
reconstruit son historique de règlements : nombre, contreparties uniques, ancienneté,
volume — et on en dérive des signaux de wash-trade / sybil.

Source : Base Blockscout v2 (base.blockscout.com) — gratuit, sans clé.
"""
from __future__ import annotations

from typing import Any

from app.sources.http_util import client, get_json

BLOCKSCOUT_BASE = "https://base.blockscout.com"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def usdc_transfers_in(addr: str, max_pages: int = 6) -> tuple[list[dict] | None, str | None]:
    """Transferts USDC ENTRANTS vers `addr` sur Base (paginé, cap max_pages*50)."""
    c = await client("blockscout", timeout=14.0)
    base_url = f"{BLOCKSCOUT_BASE}/api/v2/addresses/{addr}/token-transfers"
    params: dict[str, Any] = {"type": "ERC-20"}
    out: list[dict] = []
    err_first: str | None = None
    for _ in range(max_pages):
        data, err = await get_json(c, base_url, params=params)
        if err:
            err_first = err
            break
        items = (data or {}).get("items") or []
        for it in items:
            token = (it.get("token") or {})
            to_hash = ((it.get("to") or {}).get("hash") or "").lower()
            token_addr = (token.get("address_hash") or token.get("address") or "").lower()
            if token_addr != USDC_BASE:
                continue
            if to_hash != addr.lower():
                continue  # ne garder que l'ENTRANT (règlement reçu)
            total = it.get("total") or {}
            dec = int(_num(total.get("decimals")) or 6)
            raw = _num(total.get("value"))
            out.append({
                "from": ((it.get("from") or {}).get("hash") or "").lower(),
                "value_usdc": (raw / (10 ** dec)) if raw is not None else None,
                "ts": it.get("timestamp"),
                "tx": it.get("transaction_hash"),
            })
        next_params = (data or {}).get("next_page_params")
        if not next_params:
            break
        params = {"type": "ERC-20", **next_params}
    if not out and err_first:
        return None, err_first
    return out, None


async def usdc_transfers_ledger(addr: str, max_pages: int = 8) -> tuple[list[dict] | None, str | None]:
    """Transferts USDC ENTRANTS **et** SORTANTS pour `addr` sur Base (paginé).

    Étend `usdc_transfers_in` (qui ne garde que l'entrant) : renvoie une liste plate
    {direction: 'in'|'out', counterparty, value_usdc, ts, tx} pour le reporting comptable
    (revenus = in, dépenses = out). Additif — ne modifie pas `usdc_transfers_in`.
    """
    c = await client("blockscout", timeout=14.0)
    base_url = f"{BLOCKSCOUT_BASE}/api/v2/addresses/{addr}/token-transfers"
    params: dict[str, Any] = {"type": "ERC-20"}
    low = addr.lower()
    out: list[dict] = []
    err_first: str | None = None
    for _ in range(max_pages):
        data, err = await get_json(c, base_url, params=params)
        if err:
            err_first = err
            break
        for it in (data or {}).get("items") or []:
            token = it.get("token") or {}
            token_addr = (token.get("address_hash") or token.get("address") or "").lower()
            if token_addr != USDC_BASE:
                continue
            frm = ((it.get("from") or {}).get("hash") or "").lower()
            to = ((it.get("to") or {}).get("hash") or "").lower()
            if to == low:
                direction, counterparty = "in", frm
            elif frm == low:
                direction, counterparty = "out", to
            else:
                continue
            total = it.get("total") or {}
            dec = int(_num(total.get("decimals")) or 6)
            raw = _num(total.get("value"))
            out.append({
                "direction": direction,
                "counterparty": counterparty,
                "value_usdc": (raw / (10 ** dec)) if raw is not None else None,
                "ts": it.get("timestamp"),
                "tx": it.get("transaction_hash"),
            })
        nxt = (data or {}).get("next_page_params")
        if not nxt:
            break
        params = {"type": "ERC-20", **nxt}
    if not out and err_first:
        return None, err_first
    return out, None


# Hôtes Blockscout par chaîne EVM (réutilisés pour l'exposition mixer multi-chain).
BLOCKSCOUT_HOSTS = {
    "base": "https://base.blockscout.com", "ethereum": "https://eth.blockscout.com",
    "eth": "https://eth.blockscout.com", "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com", "arbitrum": "https://arbitrum.blockscout.com",
    "gnosis": "https://gnosis.blockscout.com",
}


async def evm_counterparties(addr: str, host: str, max_pages: int = 2) -> tuple[set[str] | None, str | None]:
    """Adresses ayant échangé des ERC-20 avec `addr` (1-hop, pour l'exposition mixer)."""
    c = await client("blockscout", timeout=12.0)
    url = f"{host}/api/v2/addresses/{addr}/token-transfers"
    params: dict[str, Any] = {"type": "ERC-20"}
    parties: set[str] = set()
    err_first: str | None = None
    for _ in range(max_pages):
        data, err = await get_json(c, url, params=params)
        if err:
            err_first = err
            break
        for it in (data or {}).get("items") or []:
            for side in ("from", "to"):
                h = ((it.get(side) or {}).get("hash") or "").lower()
                if h and h != addr.lower():
                    parties.add(h)
        nxt = (data or {}).get("next_page_params")
        if not nxt:
            break
        params = {"type": "ERC-20", **nxt}
    if not parties and err_first:
        return None, err_first
    return parties, None


async def erc20_edges(addr: str, host: str, max_pages: int = 3) -> tuple[dict[str, dict] | None, str | None]:
    """ERC-20 counterparty edges of `addr` (for multi-hop flow forensics).

    Returns (dict[counterparty] -> {in_count, out_count, tokens:set[str], last_ts}, err).
    Direction is relative to `addr`: 'in' = counterparty->addr, 'out' = addr->counterparty.
    Additive; used by /crypto/wallet-forensics. Fan-out/pages are capped by the caller.
    """
    c = await client("blockscout", timeout=12.0)
    url = f"{host}/api/v2/addresses/{addr}/token-transfers"
    params: dict[str, Any] = {"type": "ERC-20"}
    low = addr.lower()
    edges: dict[str, dict] = {}
    err_first: str | None = None
    for _ in range(max_pages):
        data, err = await get_json(c, url, params=params)
        if err:
            err_first = err
            break
        for it in (data or {}).get("items") or []:
            frm = ((it.get("from") or {}).get("hash") or "").lower()
            to = ((it.get("to") or {}).get("hash") or "").lower()
            if frm == low and to and to != low:
                cp, direction = to, "out"
            elif to == low and frm and frm != low:
                cp, direction = frm, "in"
            else:
                continue
            sym = ((it.get("token") or {}).get("symbol")) or "?"
            e = edges.setdefault(cp, {"in_count": 0, "out_count": 0, "tokens": set(), "last_ts": None})
            e["in_count" if direction == "in" else "out_count"] += 1
            e["tokens"].add(sym)
            ts = it.get("timestamp")
            if ts and (e["last_ts"] is None or ts > e["last_ts"]):
                e["last_ts"] = ts
        nxt = (data or {}).get("next_page_params")
        if not nxt:
            break
        params = {"type": "ERC-20", **nxt}
    if not edges and err_first:
        return None, err_first
    return edges, None


async def address_overview(addr: str) -> tuple[dict | None, str | None]:
    """Méta du wallet (Blockscout v2) : compteurs de tx, premier/dernier bloc si dispo."""
    c = await client("blockscout", timeout=12.0)
    data, err = await get_json(c, f"{BLOCKSCOUT_BASE}/api/v2/addresses/{addr}/counters")
    if err:
        return None, err
    return data, None
