"""Endpoint — Screening sanctions UE (liste consolidée FISMA).

Wrapper de la liste consolidée officielle des sanctions financières de l'UE
(Commission européenne / FISMA). Screene un nom (personne ou entité) contre la
liste et renvoie les correspondances avec un score de similarité et le contexte
(référence UE, type, programme, détails de désignation).

Source : EU Financial Sanctions Files (webgate.ec.europa.eu), CSV public consolidé
(token public fixe, sans clé personnelle). Tier "verdict" $0.05. TTL 6h.

C'est un endpoint de SCREENING : il retourne les MATCHES (avec score/raison),
jamais un simple oui/non binaire sans contexte.
"""

import asyncio
import csv
import io
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

router = APIRouter()

CSV_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
           "csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw")
SOURCE_NAME = "EU Consolidated Financial Sanctions List — FISMA (webgate.ec.europa.eu)"
DISCLAIMER = ("Screening indicatif contre la liste consolidée UE ; un match n'est pas une "
              "confirmation légale et requiert une revue humaine. Pas un avis de conformité.")

# Cache de la liste parsée (lourde) — TTL 6h, refresh thread-safe.
_LIST_TTL = 6 * 3600
_list_cache: dict[str, Any] = {"entries": None, "loaded_at": 0.0}
_list_lock = asyncio.Lock()

# Cache des résultats de screening par requête.
_CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_FETCH_TIMEOUT = httpx.Timeout(30.0, connect=6.0)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().split())


def _score(query_norm: str, name_norm: str) -> float:
    if not name_norm:
        return 0.0
    ratio = SequenceMatcher(None, query_norm, name_norm).ratio()
    # bonus token-set : tous les tokens de la requête présents dans le nom
    qt, nt = set(query_norm.split()), set(name_norm.split())
    token = len(qt & nt) / len(qt) if qt else 0.0
    return max(ratio, 0.5 * ratio + 0.5 * token)


def _parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    entries = []
    for row in reader:
        whole = (row.get("NameAlias_WholeName") or "").strip()
        if not whole:
            parts = [row.get("NameAlias_FirstName", ""), row.get("NameAlias_MiddleName", ""), row.get("NameAlias_LastName", "")]
            whole = " ".join(p for p in parts if p).strip()
        if not whole:
            continue
        entries.append({
            "name": whole,
            "_norm": _norm(whole),
            "subject_type": (row.get("Entity_SubjectType_ClassificationCode") or row.get("Entity_SubjectType") or "").strip(),
            "eu_reference": (row.get("Entity_EU_ReferenceNumber") or "").strip(),
            "un_id": (row.get("Entity_UnitedNationId") or "").strip(),
            "programme": (row.get("Entity_Regulation_Programme") or row.get("Entity_Regulation_Type") or "").strip(),
            "designation_details": (row.get("Entity_DesignationDetails") or "").strip()[:300],
            "publication_date": (row.get("Entity_Regulation_PublicationDate") or "").strip(),
        })
    return entries


async def _get_list() -> list[dict[str, Any]]:
    async with _list_lock:
        fresh = _list_cache["entries"] is not None and time.time() - _list_cache["loaded_at"] < _LIST_TTL
        if fresh:
            return _list_cache["entries"]
        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, headers={"User-Agent": "x402-endpoints/1.0"}) as client:
                resp = await client.get(CSV_URL)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Délai dépassé au téléchargement de la liste sanctions UE.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Liste sanctions UE indisponible.") from exc
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Source sanctions UE en erreur (HTTP {resp.status_code}).")
        entries = _parse_csv(resp.text)
        if not entries:
            raise HTTPException(status_code=502, detail="Liste sanctions UE vide ou illisible.")
        _list_cache["entries"] = entries
        _list_cache["loaded_at"] = time.time()
        return entries


async def screen_name(name: str | None, entity_type: str | None, threshold: float, limit: int) -> dict[str, Any]:
    q = (name or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Paramètre 'name' requis (≥ 2 caractères).")
    etype = (entity_type or "").strip().lower() or None
    if etype and etype not in ("person", "enterprise"):
        raise HTTPException(status_code=400, detail="'type' attendu : person ou enterprise.")
    if not (0.0 < threshold <= 1.0):
        raise HTTPException(status_code=400, detail="'threshold' attendu dans ]0, 1].")
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")

    key = f"{_norm(q)}|{etype}|{threshold}|{limit}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return {**cached[1], "cached": True}

    entries = await _get_list()
    qn = _norm(q)
    matches = []
    for e in entries:
        if etype and etype not in e["subject_type"].lower():
            continue
        sc = _score(qn, e["_norm"])
        if sc >= threshold:
            matches.append((sc, e))
    matches.sort(key=lambda x: x[0], reverse=True)

    shaped = {
        "query": q,
        "type": etype,
        "threshold": threshold,
        "match_count": len(matches),
        "matches": [{
            "name": e["name"],
            "score": round(sc, 3),
            "subject_type": e["subject_type"],
            "eu_reference": e["eu_reference"],
            "un_id": e["un_id"] or None,
            "programme": e["programme"] or None,
            "designation_details": e["designation_details"] or None,
            "publication_date": e["publication_date"] or None,
        } for sc, e in matches[:limit]],
        "list_size": len(entries),
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": DISCLAIMER,
    }
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/sanctions/screen")
async def sanctions_screen(
    name: str = Query(..., description="Name to screen (person or entity), e.g. 'Saddam Hussein'"),
    type: str | None = Query(None, description="Optional filter: 'person' or 'enterprise'"),
    threshold: float = Query(0.7, description="Min similarity score 0-1 to report a match (default 0.7)"),
    limit: int = Query(10, description="Max matches to return [1-50], e.g. 10"),
) -> JSONResponse:
    """GET /sanctions/screen?name=&type=&threshold=&limit= — screening liste consolidée UE."""
    data = await screen_name(name, type, threshold, limit)
    return JSONResponse(content=data)


@router.get("/sanctions/health")
async def sanctions_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers={"User-Agent": "x402-endpoints/1.0"}) as client:
            async with client.stream("GET", CSV_URL) as resp:  # statut sans télécharger 24 Mo
                upstream_ok = resp.status_code == 200
                detail = f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "sanctions",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "list_loaded": _list_cache["entries"] is not None,
            "list_size": len(_list_cache["entries"]) if _list_cache["entries"] else 0,
        },
    )
