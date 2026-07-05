"""Endpoint — IBAN validation (zone SEPA/EU).

Validation 100% LOCALE et déterministe d'un IBAN via l'algorithme officiel
ISO 13616 (contrôle modulo 97 = 1) + table des longueurs par pays. AUCUNE source
externe, aucune clé, aucun appel réseau.

⚠️ C'est une validation de FORMAT (l'IBAN est structurellement et
mathématiquement valide), PAS une vérification que le compte existe réellement.

Tier $0.01. Pas de cache (calcul local instantané).
"""

import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

SOURCE_NAME = "ISO 13616 mod-97 local validation (no external source)"

# Longueurs IBAN officielles par pays (registre IBAN / ISO 13616). SEPA/EU + courants.
_IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28,
    "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}

# Format BBAN connu : (offset, longueur) du code banque dans l'IBAN (après les 4 premiers car.).
_BANK_CODE_SPANS: dict[str, tuple[int, int]] = {
    "FR": (4, 5), "DE": (4, 8), "ES": (4, 4), "IT": (5, 5), "BE": (4, 3),
    "NL": (4, 4), "PT": (4, 4), "GB": (4, 4), "IE": (4, 4), "AT": (4, 5),
    "CH": (4, 5), "LU": (4, 3), "PL": (4, 8), "FI": (4, 6), "DK": (4, 4),
}


def _clean(raw: str) -> str:
    return "".join(raw.split()).replace("-", "").upper()


def _mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    digits = []
    for ch in rearranged:
        if ch.isdigit():
            digits.append(ch)
        else:  # A=10 ... Z=35
            digits.append(str(ord(ch) - 55))
    return int("".join(digits)) % 97


def _group4(iban: str) -> str:
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def validate_iban(raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="Paramètre 'iban' requis.")
    iban = _clean(raw)
    if len(iban) < 5 or len(iban) > 34:
        raise HTTPException(status_code=400, detail="'iban' de longueur aberrante (attendu 5 à 34 caractères).")
    if not iban.isalnum() or not iban.isascii():
        raise HTTPException(status_code=400, detail="'iban' contient des caractères non alphanumériques invalides.")

    country = iban[:2]
    check_digits = iban[2:4]
    base = {
        "iban": iban,
        "country_code": country,
        "check_digits": check_digits if check_digits.isdigit() else None,
        "formatted": _group4(iban),
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": "Validation de FORMAT (structure + checksum ISO 13616), PAS une vérification que le compte existe.",
    }

    if not country.isalpha():
        return {**base, "valid": False, "reason": "country_code_invalid", "bank_code": None}
    expected_len = _IBAN_LENGTHS.get(country)
    if expected_len is None:
        return {**base, "valid": False, "reason": f"unknown_country:{country}", "bank_code": None}
    if len(iban) != expected_len:
        return {**base, "valid": False,
                "reason": f"length_mismatch:expected_{expected_len}_got_{len(iban)}", "bank_code": None}
    if not check_digits.isdigit():
        return {**base, "valid": False, "reason": "check_digits_not_numeric", "bank_code": None}
    if _mod97(iban) != 1:
        return {**base, "valid": False, "reason": "checksum_failed", "bank_code": None}

    bank_code = None
    span = _BANK_CODE_SPANS.get(country)
    if span:
        off, length = span
        bank_code = iban[off:off + length]

    return {**base, "valid": True, "reason": None, "bank_code": bank_code}


@router.get("/iban/validate")
async def iban_validate(
    iban: str = Query(..., description="IBAN to validate, e.g. 'FR1420041010050500013M02606'"),
) -> JSONResponse:
    """GET /iban/validate — validation de format ISO 13616 (mod-97), 100% locale."""
    return JSONResponse(content=validate_iban(iban))


@router.get("/iban/health")
async def iban_health() -> JSONResponse:
    # Validation locale : pas de dépendance upstream. Auto-test du mod-97 sur un IBAN connu.
    self_test = validate_iban("FR1420041010050500013M02606").get("valid") is True
    return JSONResponse(
        status_code=200 if self_test else 503,
        content={
            "endpoint": "iban",
            "status": "ok" if self_test else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": True, "detail": "local algorithm"},
            "self_test": self_test,
            "countries_supported": len(_IBAN_LENGTHS),
        },
    )
