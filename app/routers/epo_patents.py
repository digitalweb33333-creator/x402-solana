"""Endpoint — Patents / EPO (brevets, Open Patent Services).

Wrapper de l'API officielle EPO OPS v3.2 : lookup bibliographique par numéro de
publication, ou recherche CQL par titre / déposant / inventeur / dates. Renvoie
pour chaque brevet une synthèse propre (numéro, pays, kind, date, titres,
déposants, inventeurs, classifications IPC/CPC, family id).

Source : EPO Open Patent Services (ops.epo.org), OAuth2 client_credentials.
Le token (~20 min) est mis en cache et renouvelé automatiquement et de façon
transparente sur 400/401 (token expiré). Quota fair-use 3,5 Go/sem : pas de retry
sur dépassement (403) ni sur erreur d'auth — on distingue les codes.

Tier $0.05 (donnée institutionnelle structurée, parsing à valeur ajoutée). TTL 24h.
"""

import asyncio
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import EPO_OPS_KEY, EPO_OPS_SECRET

router = APIRouter()

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
REST_BASE = "https://ops.epo.org/3.2/rest-services/published-data"
SOURCE_NAME = "EPO Open Patent Services v3.2 (ops.epo.org)"

_PUBNUM_RE = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{1,13}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_HEADERS = {"User-Agent": "x402-endpoints/1.0"}

# --- Cache de token OAuth2 (partagé) ---
_token: dict[str, Any] = {"value": None, "expiry": 0.0}
_token_lock = asyncio.Lock()


async def _get_token(client: httpx.AsyncClient, force: bool = False) -> str:
    async with _token_lock:
        if not force and _token["value"] and time.time() < _token["expiry"]:
            return _token["value"]
        try:
            resp = await client.post(
                AUTH_URL, data={"grant_type": "client_credentials"},
                auth=(EPO_OPS_KEY, EPO_OPS_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Échec de connexion au service d'authentification EPO.")
        if resp.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Authentification EPO refusée (clé OPS invalide).")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Échec d'obtention du token EPO (HTTP {resp.status_code}).")
        body = resp.json()
        _token["value"] = body["access_token"]
        _token["expiry"] = time.time() + int(body.get("expires_in", 1200)) - 60
        return _token["value"]


async def _authed_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response:
    """GET authentifié avec renouvellement transparent du token sur 400/401."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        token = await _get_token(client, force=(attempt > 0))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            # 400/401 = token expiré/invalide -> refresh (force) et retry, transparent
            if resp.status_code in (400, 401) and attempt < _MAX_ATTEMPTS - 1:
                # Distinguer un vrai 400 de requête d'un token expiré : on retente une fois
                # avec un token neuf ; si ça reste 400, l'appelant traitera selon le code.
                _token["value"] = None
                last_exc = httpx.HTTPStatusError("EPO auth retry", request=resp.request, response=resp)
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
                continue
            # 5xx transitoire d'OPS (hiccup serveur) -> retry avec backoff (PAS le 403 quota)
            if resp.status_code >= 500 and attempt < _MAX_ATTEMPTS - 1:
                last_exc = httpx.HTTPStatusError("EPO 5xx retry", request=resp.request, response=resp)
                await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
                continue
            return resp
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant EPO OPS.")
    if isinstance(last_exc, httpx.HTTPStatusError):
        return last_exc.response
    raise HTTPException(status_code=502, detail="Service EPO OPS indisponible.")


# --- Helpers de parsing OPS (convention nœuds texte {"$": ...}) ---
def _t(node: Any) -> Any:
    if isinstance(node, dict):
        return node.get("$")
    return node


def _as_list(node: Any) -> list[Any]:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _titles(bib: dict[str, Any]) -> dict[str, Any]:
    titles = {}
    for t in _as_list(bib.get("invention-title")):
        if isinstance(t, dict):
            lang = t.get("@lang", "??")
            titles[lang] = t.get("$")
    return titles


def _names(parties: dict[str, Any], group: str, item_key: str, name_key: str) -> list[str]:
    seen: list[str] = []
    for entry in _as_list((parties.get(group) or {}).get(item_key)):
        if entry.get("@data-format") == "original":
            continue  # éviter les doublons : garder la forme epodoc normalisée
        name = _t(((entry.get(name_key) or {}).get("name")))
        if name and name not in seen:
            seen.append(name)
    return seen


def _ipc(bib: dict[str, Any]) -> list[str]:
    out = []
    for t in _as_list((bib.get("classification-ipc") or {}).get("text")):
        val = _t(t)
        if val and val not in out:
            out.append(val)
    return out


def _cpc(bib: dict[str, Any]) -> list[str]:
    out = []
    for c in _as_list((bib.get("patent-classifications") or {}).get("patent-classification")):
        if not isinstance(c, dict):
            continue
        try:
            code = (f"{_t(c['section'])}{_t(c['class'])}{_t(c['subclass'])}"
                    f"{_t(c['main-group'])}/{_t(c['subgroup'])}")
        except (KeyError, TypeError):
            continue
        if code not in out:
            out.append(code)
    return out


def _shape_doc(doc: dict[str, Any]) -> dict[str, Any]:
    bib = doc.get("bibliographic-data", {}) or {}
    parties = bib.get("parties", {}) or {}
    pub_date = None
    for did in _as_list((bib.get("publication-reference") or {}).get("document-id")):
        if isinstance(did, dict) and did.get("@document-id-type") == "docdb":
            pub_date = _t(did.get("date"))
            break
    return {
        "publication_number": f"{doc.get('@country', '')}{doc.get('@doc-number', '')}{doc.get('@kind', '')}",
        "country": doc.get("@country"),
        "doc_number": doc.get("@doc-number"),
        "kind_code": doc.get("@kind"),
        "publication_date": pub_date,
        "family_id": doc.get("@family-id"),
        "titles": _titles(bib),
        "applicants": _names(parties, "applicants", "applicant", "applicant-name"),
        "inventors": _names(parties, "inventors", "inventor", "inventor-name"),
        "ipc_classifications": _ipc(bib),
        "cpc_classifications": _cpc(bib),
    }


def _extract_docs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    docs = ((payload.get("ops:world-patent-data") or {}).get("exchange-documents") or {}).get("exchange-document")
    return _as_list(docs)


async def _biblio(client: httpx.AsyncClient, number: str) -> dict[str, Any]:
    safe = number.strip().upper()
    if not _PUBNUM_RE.match(safe):
        raise HTTPException(status_code=400, detail="'publication_number' invalide (ex. 'EP1000000').")
    resp = await _authed_get(client, f"{REST_BASE}/publication/epodoc/{safe}/biblio")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Brevet introuvable pour le numéro {safe}.")
    if resp.status_code == 403:
        raise HTTPException(status_code=503, detail="Quota fair-use EPO dépassé, réessayer plus tard.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse EPO inattendue (HTTP {resp.status_code}).")
    docs = _extract_docs(resp.json())
    if not docs:
        raise HTTPException(status_code=404, detail=f"Aucune donnée bibliographique pour {safe}.")
    return {
        "mode": "publication",
        "count": len(docs),
        "patents": [_shape_doc(d) for d in docs],
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _build_cql(title, applicant, inventor, date_from, date_to) -> str:
    parts = []
    if title:
        parts.append(f'ti="{title.replace(chr(34), " ").strip()}"')
    if applicant:
        parts.append(f'pa="{applicant.replace(chr(34), " ").strip()}"')
    if inventor:
        parts.append(f'in="{inventor.replace(chr(34), " ").strip()}"')
    if date_from or date_to:
        lo = (date_from or date_to).replace("-", "")
        hi = (date_to or date_from).replace("-", "")
        parts.append(f'pd within "{lo} {hi}"')
    return " and ".join(parts)


async def _search(client: httpx.AsyncClient, title, applicant, inventor, date_from, date_to, limit) -> dict[str, Any]:
    for label, val in (("date_from", date_from), ("date_to", date_to)):
        if val and not _DATE_RE.match(val):
            raise HTTPException(status_code=400, detail=f"'{label}' doit être une date ISO YYYY-MM-DD.")
    cql = _build_cql(title, applicant, inventor, date_from, date_to)
    if not cql:
        raise HTTPException(
            status_code=400,
            detail="Au moins un critère requis : publication_number, title, applicant, inventor ou date_from/date_to.")

    resp = await _authed_get(client, f"{REST_BASE}/search", params={"q": cql, "Range": f"1-{limit}"})
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail="Requête de recherche EPO invalide (syntaxe CQL).")
    if resp.status_code == 404:
        return {"mode": "search", "query": cql, "total_results": 0, "count": 0, "patents": [],
                "source": SOURCE_NAME, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if resp.status_code == 403:
        raise HTTPException(status_code=503, detail="Quota fair-use EPO dépassé, réessayer plus tard.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Réponse EPO inattendue (HTTP {resp.status_code}).")

    sr = ((resp.json().get("ops:world-patent-data") or {}).get("ops:biblio-search") or {})
    total = sr.get("@total-result-count")
    refs = _as_list((sr.get("ops:search-result") or {}).get("ops:publication-reference"))

    docdb_numbers = []
    for ref in refs:
        did = ref.get("document-id") if isinstance(ref, dict) else None
        if isinstance(did, dict) and did.get("@document-id-type") == "docdb":
            country, num, kind = _t(did.get("country")), _t(did.get("doc-number")), _t(did.get("kind"))
            if country and num:
                docdb_numbers.append(f"{country}.{num}." + (kind or ""))

    patents: list[dict[str, Any]] = []
    if docdb_numbers:
        # Enrichissement bibliographique en UN appel batché (économe en quota).
        try:
            bresp = await _authed_get(client, f"{REST_BASE}/publication/docdb/{','.join(docdb_numbers)}/biblio")
            if bresp.status_code == 200:
                patents = [_shape_doc(d) for d in _extract_docs(bresp.json())]
        except HTTPException:
            patents = []
    if not patents:
        # Fallback honnête : références brutes sans enrichissement.
        for ref in refs:
            did = ref.get("document-id") if isinstance(ref, dict) else {}
            patents.append({
                "publication_number": f"{_t(did.get('country')) or ''}{_t(did.get('doc-number')) or ''}{_t(did.get('kind')) or ''}",
                "country": _t(did.get("country")), "doc_number": _t(did.get("doc-number")),
                "kind_code": _t(did.get("kind")), "family_id": ref.get("@family-id"),
            })

    return {
        "mode": "search",
        "query": cql,
        "total_results": int(total) if total is not None else None,
        "count": len(patents),
        "patents": patents,
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def patents_search(publication_number, title, applicant, inventor, date_from, date_to, limit) -> dict[str, Any]:
    if not (1 <= limit <= 50):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 50].")

    if publication_number and publication_number.strip():
        cache_key = f"pub|{publication_number.strip().upper()}"
    else:
        cache_key = f"search|{title}|{applicant}|{inventor}|{date_from}|{date_to}|{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
        if publication_number and publication_number.strip():
            data = await _biblio(client, publication_number)
        else:
            data = await _search(client, title, applicant, inventor, date_from, date_to, limit)
    _cache[cache_key] = (time.time(), data)
    return {**data, "cached": False}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


@router.get("/patents/search")
async def patents_search_endpoint(
    publication_number: str | None = Query(None, description="If set, return bibliographic data for this number, e.g. 'EP1000000'"),
    title: str | None = Query(None, description="Title keyword (CQL ti=), e.g. 'quantum computing'"),
    applicant: str | None = Query(None, description="Applicant / assignee (CQL pa=), e.g. 'Siemens'"),
    inventor: str | None = Query(None, description="Inventor name (CQL in=), e.g. 'Shannon'"),
    date_from: str | None = Query(None, description="Publication date from, ISO YYYY-MM-DD"),
    date_to: str | None = Query(None, description="Publication date to, ISO YYYY-MM-DD"),
    limit: int = Query(20, description="Max patents [1-50], e.g. 20"),
) -> JSONResponse:
    """GET /patents/search — recherche & lookup biblio de brevets (EPO Open Patent Services)."""
    data = await patents_search(publication_number, title, applicant, inventor, date_from, date_to, limit)
    return JSONResponse(content=data)


@router.get("/patents/health")
async def patents_health() -> JSONResponse:
    """Santé EPO : vérifie l'obtention du token OAuth2 (n'entame PAS le quota data)."""
    upstream_ok = False
    detail = "unreachable"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), headers=_HEADERS) as client:
            await _get_token(client)
            upstream_ok = True
            detail = "token OK"
    except HTTPException as exc:
        detail = f"{exc.status_code} {exc.detail}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "patents",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(EPO_OPS_KEY and EPO_OPS_SECRET),
            "cache_entries": len(_cache),
        },
    )
