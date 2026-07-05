"""Log de règlement par route — comble le trou d'attribution transaction -> endpoint.

Le service est stateless (pas de DB, cf CLAUDE.md) et on-chain un règlement ne porte que
le MONTANT, pas la route. Résultat : impossible d'attribuer une tx à un endpoint précis
quand plusieurs endpoints partagent un prix. Ce module émet, à CHAQUE settle réussi, une
ligne structurée reliant `route <-> tx_hash <-> payer <-> montant <-> horodatage`.

- Sortie 1 (primaire, durable via la plateforme) : une ligne JSON sur stdout préfixée
  `X402_SETTLE ` — capturée par les logs Render / n'importe quel log drain. Greppable, et
  joignable au on-chain par tx_hash pour une attribution par endpoint 100 % déterministe.
- Sortie 2 (best-effort) : append dans un fichier JSONL (`SETTLEMENT_LOG_PATH`, défaut
  /tmp/x402_settlements.jsonl). Éphémère sur Render mais utile en local/dev et si un disque
  persistant est monté plus tard.

Le logging ne DOIT JAMAIS casser la réponse de paiement : tout est encapsulé (best-effort).
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

_LOG_PATH = (os.getenv("SETTLEMENT_LOG_PATH", "") or "/tmp/x402_settlements.jsonl").strip()
_STDOUT_TAG = "X402_SETTLE"


def log_settlement(
    *,
    route: str,
    method: str,
    tx: str | None,
    payer: str | None,
    amount: str | None,
    network: str | None,
    success: bool,
) -> None:
    """Enregistre un règlement (best-effort, ne lève jamais)."""
    try:
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "route": route,
            "method": method,
            "tx": tx,
            "payer": payer,
            "amount": amount,      # montant en USDC (string, ex "0.25")
            "network": network,
            "success": success,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        # Sortie 1 : stdout (Render logs) — flush immédiat pour ne rien perdre au redéploy.
        print(f"{_STDOUT_TAG} {line}", file=sys.stdout, flush=True)
        # Sortie 2 : fichier append-only (best-effort).
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    except Exception:  # noqa: BLE001 — le log ne doit jamais casser le paiement
        pass
