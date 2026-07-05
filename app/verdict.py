"""Enveloppe machine-readable commune aux 7 endpoints « décision pour agent ».

Les 5 règles (cf prompt) : (1) `verdict` énuméré en haut, (2) `confidence` 0-1 +
`reasons[]` = objets {code,label,weight}, (3) `data_freshness` {timestamp, age_seconds}
+ `deterministic` + `sources[]`, (4) schéma strict + codes d'erreur énumérés,
(5) droit de s'abstenir (`verdict: "ABSTAIN"`).

Ces helpers garantissent la MÊME forme partout pour qu'un agent route la réponse
sans raisonner.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def age_seconds(iso_str: str | None) -> int | None:
    """Âge en secondes d'un timestamp ISO (UTC). None si non parsable."""
    if not iso_str:
        return None
    s = iso_str.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int(datetime.now(timezone.utc).timestamp() - dt.timestamp()))
    except ValueError:
        return None


def reason(code: str, label: str, weight: float) -> dict[str, Any]:
    """Une raison structurée. weight signé : >0 = vers le risque/négatif, <0 = rassurant."""
    return {"code": code, "label": label, "weight": round(weight, 4)}


def freshness(data_iso: str | None, *, deterministic: bool, sources: list[str],
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bloc `data_freshness` standard."""
    block = {
        "as_of": data_iso or now_iso(),
        "age_seconds": age_seconds(data_iso) if data_iso else 0,
        "retrieved_at": now_iso(),
        "deterministic": deterministic,
        "sources": sources,
    }
    if extra:
        block.update(extra)
    return block


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
