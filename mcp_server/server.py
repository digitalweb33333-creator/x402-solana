"""Serveur MCP (low-level SDK) exposant le catalogue x402-endpoints.

- list_tools : un outil MCP par endpoint x402 (depuis .well-known/x402.json).
- call_tool  : proxy vers l'endpoint déployé.
    * AUTO-PAY  si un wallet acheteur financé est configuré -> vraie donnée.
    * DÉCOUVERTE sinon -> conditions de paiement décodées (402) + exemple de sortie.

Transports : stdio (Claude Desktop / Cursor / LangChain) et HTTP streamable
(remote / self-host, montable dans une app Starlette/FastAPI).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

import mcp.types as types
from mcp.server.lowlevel import Server

from .catalog import ToolSpec, load_tool_specs

# --- Configuration (env) ---
DEFAULT_BASE_URL = "https://x402-solana-cva8.onrender.com"
BASE_URL = os.getenv("X402_BASE_URL", os.getenv("PUBLIC_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
NETWORK = os.getenv("X402_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp").strip()
# Accepte X402_BUYER_PRIVATE_KEY ou BUYER_PRIVATE_KEY (compat .env du projet).
_BUYER_KEY = (
    os.getenv("X402_BUYER_PRIVATE_KEY") or os.getenv("BUYER_PRIVATE_KEY") or ""
).strip()
# AUTO-PAY actif seulement si une clé est présente ET non désactivé explicitement.
AUTO_PAY = bool(_BUYER_KEY) and os.getenv("X402_AUTO_PAY", "1").strip() not in (
    "0",
    "false",
    "no",
)
_HTTP_TIMEOUT = httpx.Timeout(40.0, connect=10.0)

SERVER_NAME = "x402-endpoints"
SERVER_INSTRUCTIONS = (
    "Paid tools wrapping official real-world APIs and crypto pre-trade data, "
    "billed per call via the x402 protocol (USDC on Base mainnet). Each tool's "
    "description states its price. With a funded Base wallet configured, calls "
    "return live data automatically; otherwise they return the exact payment "
    "terms so a wallet can complete payment."
)


def _build_buyer_client() -> Any | None:
    """Construit le client httpx x402 acheteur (auto-pay), ou None si pas de clé."""
    if not AUTO_PAY:
        return None
    from eth_account import Account
    from x402 import x402Client
    from x402.http.clients import x402HttpxClient
    from x402.mechanisms.evm.exact import register_exact_evm_client

    signer = Account.from_key(_BUYER_KEY)
    client = x402Client()
    register_exact_evm_client(client, signer, networks=NETWORK)
    # x402HttpxClient = httpx.AsyncClient qui gère 402 -> paie -> rejoue.
    return x402HttpxClient(client, timeout=_HTTP_TIMEOUT, follow_redirects=True)


def _clean_params(arguments: dict[str, Any]) -> dict[str, Any]:
    """Retire les paramètres absents (None / chaîne vide) avant l'appel HTTP."""
    return {k: v for k, v in arguments.items() if v is not None and v != ""}


def _decode_payment_required(resp: httpx.Response) -> dict[str, Any] | None:
    """Décode le header `payment-required` (base64 JSON) d'une réponse 402."""
    header = resp.headers.get("payment-required") or resp.headers.get("Payment-Required")
    if not header:
        return None
    try:
        return json.loads(base64.b64decode(header).decode("utf-8"))
    except Exception:
        return None


def _discovery_payload(spec: ToolSpec, resp: httpx.Response) -> dict[str, Any]:
    """Construit la réponse « paiement requis » lisible par l'agent (mode découverte)."""
    decoded = _decode_payment_required(resp) or {}
    accepts = decoded.get("accepts") or []
    accept = accepts[0] if accepts else {}
    return {
        "x402_payment_required": True,
        "tool": spec.name,
        "resource": f"{BASE_URL}{spec.path}",
        "price": spec.price,
        "network": accept.get("network", spec.network or NETWORK),
        "asset": accept.get("asset", spec.asset),
        "pay_to": accept.get("payTo") or accept.get("pay_to") or spec.pay_to,
        "scheme": accept.get("scheme", "exact"),
        "max_timeout_seconds": accept.get("maxTimeoutSeconds", 300),
        "how_to_pay": (
            "This endpoint requires an x402 micropayment. Use an x402-aware HTTP "
            "client with a funded Base (USDC) wallet, or configure a buyer key in "
            "this MCP server (X402_BUYER_PRIVATE_KEY) to auto-pay and receive live data."
        ),
        "example_output": spec.output_example,
    }


def build_server(specs: list[ToolSpec] | None = None) -> Server:
    """Construit le serveur MCP low-level avec list_tools / call_tool."""
    specs = specs if specs is not None else load_tool_specs()
    by_name: dict[str, ToolSpec] = {s.name: s for s in specs}
    buyer_client = _build_buyer_client()

    server: Server = Server(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=s.name,
                description=s.description,
                inputSchema=s.input_schema,
            )
            for s in specs
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = by_name.get(name)
        if spec is None:
            return {"error": f"Unknown tool: {name}"}
        params = _clean_params(arguments or {})
        url = f"{BASE_URL}{spec.path}"

        if buyer_client is not None:
            # AUTO-PAY : le client x402 paie le 402 et rejoue, renvoie la vraie donnée.
            try:
                resp = (await buyer_client.post(url, json=params) if spec.method == "POST"
                        else await buyer_client.get(url, params=params))
            except Exception as exc:  # erreur paiement / réseau
                return {
                    "error": "x402 payment or upstream request failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "tool": spec.name,
                    "resource": url,
                }
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text, "tool": spec.name}
            if resp.status_code == 402:
                # Paiement non abouti (ex. wallet non financé) -> conditions de paiement.
                return _discovery_payload(spec, resp)
            return {
                "error": f"Upstream returned HTTP {resp.status_code}",
                "detail": resp.text[:500],
                "tool": spec.name,
            }

        # DÉCOUVERTE : appel non payé -> 402 attendu -> on renvoie les conditions.
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            try:
                resp = (await client.post(url, json=params) if spec.method == "POST"
                        else await client.get(url, params=params))
            except Exception as exc:
                return {
                    "error": "request failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "tool": spec.name,
                    "resource": url,
                }
        if resp.status_code == 402:
            return _discovery_payload(spec, resp)
        if resp.status_code == 200:
            # endpoint non gated (ex. health) — improbable ici, mais on relaie.
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text, "tool": spec.name}
        return {
            "error": f"Unexpected HTTP {resp.status_code}",
            "detail": resp.text[:500],
            "tool": spec.name,
        }

    return server


def mode_summary() -> dict[str, Any]:
    """Résumé de configuration (pour logs / diagnostic), sans exposer de secret."""
    return {
        "server": SERVER_NAME,
        "base_url": BASE_URL,
        "network": NETWORK,
        "mode": "auto-pay" if AUTO_PAY else "discovery",
        "buyer_key_present": bool(_BUYER_KEY),
        "tool_count": len(load_tool_specs()),
    }
