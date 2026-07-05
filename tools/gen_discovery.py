"""Génère les fichiers de découverte Solana depuis ROUTE_META (source unique) :
  - .well-known/x402.json      (CDP Bazaar / x402scan)
  - .well-known/agent-card.json (A2A)
  - llms.txt                    (index sémantique machine-readable)

Format répliqué à l'identique du projet Base, réseau/asset/pay_to = Solana.
Idempotent : ré-exécutable à chaque changement de _routes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.main import ROUTE_META, app
from app.config import (
    FACILITATOR_URL,
    NETWORK,
    SOLANA_SELLER_ADDRESS,
    SOLANA_USDC_MINT,
)

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_URL = os.getenv("PUBLIC_BASE_URL", "https://x402-solana.onrender.com").rstrip("/")
ASSET = {"address": SOLANA_USDC_MINT, "symbol": "USDC", "decimals": 6}
SERVICE_NAME = "x402-solana"
SERVICE_DESC = app.description


def _atomic(price: str) -> str:
    return str(int(round(float(str(price).lstrip("$")) * 1_000_000)))


def _query_params(input_schema: dict) -> dict:
    props = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    out = {}
    for name, spec in props.items():
        entry = {k: v for k, v in spec.items() if k in ("type", "description", "pattern", "enum")}
        entry["required"] = name in required
        out[name] = entry
    return out


def _skill_id(path: str, name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-").replace("(", "").replace(")", "")


def build_x402() -> dict:
    resources = []
    for path, m in ROUTE_META.items():
        resources.append({
            "resource": path,
            "method": "GET",
            "description": m["description"],
            "mimeType": "application/json",
            "price": m["price"],
            "amount": _atomic(m["price"]),
            "scheme": "exact",
            "network": NETWORK,
            "asset": SOLANA_USDC_MINT,
            "pay_to": SOLANA_SELLER_ADDRESS,
            "maxTimeoutSeconds": 300,
            "tags": m["tags"],
            "input": {"type": "http", "method": "GET", "queryParams": _query_params(m["input_schema"])},
            "output": {"type": "json", "example": m["output_example"]},
        })
    return {
        "x402Version": 2,
        "name": SERVICE_NAME,
        "description": SERVICE_DESC,
        "pay_to": SOLANA_SELLER_ADDRESS,
        "network": NETWORK,
        "asset": ASSET,
        "facilitator": FACILITATOR_URL,
        "resources": resources,
    }


def build_agent_card() -> dict:
    skills = []
    for path, m in ROUTE_META.items():
        skills.append({
            "id": _skill_id(path, m["service_name"]),
            "name": m["service_name"],
            "description": m["description"],
            "tags": m["tags"],
            "examples": [
                f"Call {path} with {json.dumps(m['input_example'])}",
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
            "endpoint": {
                "method": "GET",
                "path": path,
                "queryParams": _query_params(m["input_schema"]),
            },
        })
    return {
        "protocolVersion": "0.3.0",
        "name": SERVICE_NAME,
        "description": SERVICE_DESC,
        "url": PUBLIC_URL,
        "preferredTransport": "JSONRPC",
        "provider": {"organization": SERVICE_NAME, "url": PUBLIC_URL},
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "payments": {
            "protocol": "x402",
            "network": NETWORK,
            "asset": ASSET,
            "payTo": SOLANA_SELLER_ADDRESS,
            "facilitator": FACILITATOR_URL,
        },
        "skills": skills,
    }


def build_llms_txt() -> str:
    lines = [
        f"# {SERVICE_NAME}",
        "",
        f"> {SERVICE_DESC}",
        "",
        f"- Payment rail: x402 exact, USDC on Solana mainnet ({NETWORK}).",
        f"- Asset: USDC mint {SOLANA_USDC_MINT} (6 decimals). Pay to: {SOLANA_SELLER_ADDRESS}.",
        f"- Facilitator: {FACILITATOR_URL} (gasless for the buyer — the facilitator is fee payer).",
        f"- Base URL: {PUBLIC_URL}. No API key — payment is authentication.",
        "",
        "## Endpoints",
        "",
    ]
    for path, m in ROUTE_META.items():
        params = ", ".join(
            f"{k}{'*' if v.get('required') else ''}"
            for k, v in _query_params(m["input_schema"]).items()
        )
        lines += [
            f"### GET {path}  ({m['price']})",
            f"{m['description']}",
            f"- Input (query): {params or 'none'}",
            f"- Example: GET {path}?" + "&".join(f"{k}={v}" for k, v in _flatten(m["input_example"]).items()),
            f"- Tags: {', '.join(m['tags'])}",
            "",
        ]
    return "\n".join(lines)


def _flatten(example: dict) -> dict:
    out = {}
    for k, v in (example or {}).items():
        out[k] = v if not isinstance(v, (dict, list)) else json.dumps(v)
    return out


def main() -> None:
    wk = ROOT / ".well-known"
    wk.mkdir(exist_ok=True)
    (wk / "x402.json").write_text(json.dumps(build_x402(), indent=2, ensure_ascii=False), encoding="utf-8")
    (wk / "agent-card.json").write_text(json.dumps(build_agent_card(), indent=2, ensure_ascii=False), encoding="utf-8")
    (ROOT / "llms.txt").write_text(build_llms_txt(), encoding="utf-8")
    print(f"generated: .well-known/x402.json ({len(ROUTE_META)} resources), .well-known/agent-card.json, llms.txt")


if __name__ == "__main__":
    main()
