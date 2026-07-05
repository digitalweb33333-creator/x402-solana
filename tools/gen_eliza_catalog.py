"""Génère eliza-plugin/src/catalog.json (format plugin ElizaOS) depuis ROUTE_META.

Réseau/asset/payTo = Solana. Un endpoint -> une action X402_<TOOL>. Idempotent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.main import ROUTE_META
from app.config import FACILITATOR_URL, NETWORK, SOLANA_SELLER_ADDRESS, SOLANA_USDC_MINT

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://x402-solana-cva8.onrender.com").rstrip("/")


def _tool(path: str) -> str:
    return path.strip("/").replace("/", "_").replace("-", "_")


def main() -> None:
    endpoints = []
    for path, m in ROUTE_META.items():
        tool = _tool(path)
        endpoints.append({
            "tool": tool,
            "action": "X402_" + tool.upper(),
            "path": path,
            "method": "GET",
            "price": m["price"],
            "description": m["description"],
            "llm_usage_prompt": f"{m['service_name']}: {m['description']} Input: "
                                + ", ".join((m['input_schema'].get('properties') or {}).keys()) + ".",
            "tags": m["tags"],
            "inputSchema": m["input_schema"],
            "outputExample": m["output_example"],
            "payTo": SOLANA_SELLER_ADDRESS,
            "network": NETWORK,
            "asset": SOLANA_USDC_MINT,
        })
    catalog = {
        "name": "x402-solana",
        "baseUrl": BASE_URL,
        "network": NETWORK,
        "asset": SOLANA_USDC_MINT,
        "payTo": SOLANA_SELLER_ADDRESS,
        "facilitator": FACILITATOR_URL,
        "endpoints": endpoints,
    }
    out = ROOT / "eliza-plugin" / "src" / "catalog.json"
    out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} ({len(endpoints)} endpoints, network={NETWORK})")


if __name__ == "__main__":
    main()
