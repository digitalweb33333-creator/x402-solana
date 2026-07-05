"""Client Claude Haiku — compose un livrable JSON STRICT pour les endpoints premium.

Pourquoi un client maison via httpx (et pas le SDK `anthropic`) : le repo cure ses
dépendances (cf requirements.txt) et httpx est déjà là. Zéro dépendance ajoutée,
même discipline que app/sources/http_util.py.

Garantie de schéma : on force un tool-use unique (`tool_choice` = ce tool, `strict: true`,
`additionalProperties: false`, `required` sur tous les champs). L'API renvoie alors un
bloc `tool_use.input` validé contre le schéma → sortie DÉTERMINISTE (vocabulaire fermé,
types constants, champs toujours présents). Température basse pour la reproductibilité.

« Jamais de 500 nu » : `compose()` ne lève jamais. Il renvoie (data, None) en cas de
succès, (None, "raison") sinon. L'appelant décide alors de dégrader en heuristique.
Modèle : Haiku 4.5 (claude-haiku-4-5) — supporte temperature + tool use ; PAS effort.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

_API_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


def llm_available() -> bool:
    """True si la clé Haiku est configurée sur cet hôte."""
    return bool(ANTHROPIC_API_KEY)


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": _VERSION,
                        "content-type": "application/json",
                    },
                    limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60.0),
                )
    return _client


async def compose(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    tool_name: str = "emit",
    tool_description: str = "Emit the structured deliverable. Fill every field.",
    max_tokens: int = 1536,
    temperature: float = 0.2,
    attempts: int = 3,
    backoff: float = 0.6,
) -> tuple[dict[str, Any] | None, str | None]:
    """Compose un objet JSON validé contre `schema` via Claude Haiku.

    `schema` : JSON Schema d'objet (type=object, properties, required, additionalProperties:false).
    Renvoie (data, None) ou (None, 'raison'). Ne lève jamais.
    """
    if not ANTHROPIC_API_KEY:
        return None, "anthropic_key_missing"

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [{
            "name": tool_name,
            "description": tool_description,
            "strict": True,
            "input_schema": schema,
        }],
        "tool_choice": {"type": "tool", "name": tool_name},
    }

    client = await _get_client()
    last = "unknown"
    for attempt in range(attempts):
        try:
            r = await client.post(_API_URL, json=body)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    return None, "invalid_json_response"
                # Bloc forcé : on extrait le tool_use.input (déjà validé par l'API).
                for block in data.get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == tool_name:
                        out = block.get("input")
                        if isinstance(out, dict):
                            return out, None
                        return None, "tool_use_input_not_object"
                # Refus de sûreté éventuel → pas de tool_use.
                if data.get("stop_reason") == "refusal":
                    return None, "model_refusal"
                return None, "no_tool_use_block"
            if r.status_code in (401, 403):
                return None, f"auth_error_{r.status_code}"  # clé invalide → inutile de réessayer
            if r.status_code == 400:
                return None, "bad_request"
            last = f"HTTP {r.status_code}"
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, last
