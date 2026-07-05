"""Lint découvrabilité des routes x402-solana (Étape 3).

Échoue (exit 1) si, pour une route gated :
  1. `description` > 500 caractères (borne DURE du facilitator CDP → settle rejeté).
  2. un mot/patron FRANÇAIS apparaît dans description / service_name / tags
     (métadonnées publiques = ANGLAIS uniquement).
  3. un artefact d'échappement parasite (backslash isolé) pollue la description.

Se branche sur app.main._routes (source de vérité unique).
"""
from __future__ import annotations

import re
import sys

from app.main import _routes, DESCRIPTION_MAX_CHARS

# Marqueurs français fréquents (mots-outils + accents) — sûrs, pas de faux positif EN.
FRENCH_TOKENS = [
    r"\bdonn[ée]es?\b", r"\bvérif\w*", r"\bofficielle?s?\b", r"\brech\w*", r"\bpaiement\b",
    r"\bmontant\b", r"\butilis\w*", r"\bréseau\b", r"\bavec\b", r"\bpour\b", r"\bsans\b",
    r"\bsociété\b", r"\bentreprise\b", r"\brenseign\w*", r"\bfrançais\b", r"\bconformité\b",
    r"\b592\b",  # placeholder never matched; keeps list non-empty structure
]
ACCENTS = re.compile(r"[àâäçéèêëîïôöùûüÿœ]", re.IGNORECASE)
LONE_BACKSLASH = re.compile(r"\\(?![ntr\"'\\/u])")  # backslash non-échappement légitime


def main() -> int:
    problems: list[str] = []
    for key, rc in _routes.items():
        opt = rc.accepts[0] if isinstance(rc.accepts, (list, tuple)) else rc.accepts
        desc = rc.description or ""
        name = rc.service_name or ""
        tags = list(rc.tags or [])
        blob = " ".join([desc, name, " ".join(tags)])

        # 1. longueur
        if len(desc) > DESCRIPTION_MAX_CHARS:
            problems.append(f"{key}: description {len(desc)} > {DESCRIPTION_MAX_CHARS} chars")

        # 2. français
        for pat in FRENCH_TOKENS:
            m = re.search(pat, blob, re.IGNORECASE)
            if m:
                problems.append(f"{key}: mot français probable '{m.group(0)}'")
        if ACCENTS.search(blob):
            problems.append(f"{key}: accent français dans les métadonnées")

        # 3. backslash parasite
        if LONE_BACKSLASH.search(desc):
            problems.append(f"{key}: backslash parasite dans la description")

    print(f"routes vérifiées: {len(_routes)}")
    if problems:
        print("LINT FAIL:")
        for p in problems:
            print("  -", p)
        return 1
    # récap longueurs
    for key, rc in _routes.items():
        print(f"  OK {key}  (desc {len(rc.description)} chars)")
    print("LINT PASS — descriptions anglais-only, toutes <= 500 chars, aucun artefact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
