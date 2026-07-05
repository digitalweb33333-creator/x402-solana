"""Construction des specs d'outils MCP à partir de .well-known/x402.json.

Chaque ressource x402 (un endpoint payant) devient un outil MCP avec :
- un nom dérivé du chemin (/crypto/token-safety -> crypto_token_safety),
- une description sémantique dense (description + prix + llm_usage_prompt),
- un inputSchema JSON Schema construit depuis input.queryParams,
- les métadonnées de paiement (path, prix, méthode) pour le handler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Racine du projet = parent de ce package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CATALOG = _PROJECT_ROOT / ".well-known" / "x402.json"


@dataclass
class ToolSpec:
    """Spécification d'un outil MCP adossé à un endpoint x402."""

    name: str
    path: str
    method: str
    price: str
    description: str
    input_schema: dict[str, Any]
    output_example: Any = None
    tags: list[str] = field(default_factory=list)
    pay_to: str = ""
    network: str = ""
    asset: str = ""


def tool_name_from_path(path: str) -> str:
    """/crypto/token-safety -> crypto_token_safety (≤64 chars, [a-z0-9_])."""
    slug = path.strip("/").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:64]


def _build_input_schema(query_params: dict[str, Any]) -> dict[str, Any]:
    """input.queryParams (x402.json) -> JSON Schema object pour l'outil MCP."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, meta in query_params.items():
        meta = meta or {}
        prop: dict[str, Any] = {"type": meta.get("type", "string")}
        if meta.get("description"):
            prop["description"] = meta["description"]
        if meta.get("pattern"):
            prop["pattern"] = meta["pattern"]
        properties[name] = prop
        if meta.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _build_description(resource: dict[str, Any]) -> str:
    """Description riche : phrase d'autorité + prix + prompt d'usage LLM."""
    base = (resource.get("description") or "").strip()
    price = resource.get("price", "")
    usage = (resource.get("llm_usage_prompt") or "").strip()
    parts = [base, f"Price: {price} per call (x402 payment, USDC on Base mainnet)."]
    if usage:
        parts.append(usage)
    return " ".join(p for p in parts if p)


def build_tool_spec(resource: dict[str, Any]) -> ToolSpec:
    query_params = (resource.get("input") or {}).get("queryParams", {}) or {}
    return ToolSpec(
        name=tool_name_from_path(resource["resource"]),
        path=resource["resource"],
        method=(resource.get("method") or "GET").upper(),
        price=resource.get("price", ""),
        description=_build_description(resource),
        input_schema=_build_input_schema(query_params),
        output_example=(resource.get("output") or {}).get("example"),
        tags=resource.get("tags", []) or [],
        pay_to=resource.get("pay_to", ""),
        network=resource.get("network", ""),
        asset=resource.get("asset", ""),
    )


def load_tool_specs(catalog_path: Path | None = None) -> list[ToolSpec]:
    """Charge x402.json et renvoie la liste des specs d'outils (1 par endpoint)."""
    path = catalog_path or _DEFAULT_CATALOG
    data = json.loads(path.read_text(encoding="utf-8"))
    resources = data.get("resources", [])
    specs = [build_tool_spec(r) for r in resources]
    # Garde-fou : noms uniques (sinon collision d'outils MCP).
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"Nom d'outil MCP dupliqué : {spec.name} ({spec.path})")
        seen.add(spec.name)
    return specs
