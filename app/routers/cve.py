"""Endpoint — CVE lookup (vulnérabilités de sécurité).

Wrapper de l'API officielle NVD (NIST National Vulnerability Database) v2.0 :
recherche une CVE précise par identifiant, ou par mot-clé/produit.

Source : NVD NIST (services.nvd.nist.gov), publique, sans clé.
NOTE : sans clé, NVD limite le débit (~5 req/30s). Une clé gratuite NVD relève
la limite (~50 req/30s) — à prendre si le volume augmente (cf rapport au démarrage).
Tier "verif" $0.01. TTL 1h.
"""

import asyncio
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import NVD_API_KEY

router = APIRouter()

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
SOURCE_NAME = "NVD — NIST National Vulnerability Database v2.0 (services.nvd.nist.gov)"
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)


def _headers() -> dict[str, str]:
    """En-têtes NVD : la clé API (header `apiKey`) relève le rate limit ~5→~50 req/30s."""
    h = {"User-Agent": "x402-endpoints/1.0"}
    if NVD_API_KEY:
        h["apiKey"] = NVD_API_KEY
    return h


# Client httpx partagé (connexion NVD gardée chaude -> évite un handshake TLS par appel,
# qui sous rafale provoquait des ConnectTimeout/ReadTimeout). Créé paresseusement.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    headers=_headers(),
                    limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60.0),
                )
    return _client

_CACHE_TTL = 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_TIMEOUT = httpx.Timeout(15.0, connect=4.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # NVD rate-limit -> backoff plus large


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    cve = item.get("cve", {})
    descs = cve.get("descriptions", [])
    desc_en = next((d["value"] for d in descs if d.get("lang") == "en"), None)

    metrics = cve.get("metrics", {})
    cvss = None
    for mkey in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        if metrics.get(mkey):
            data = metrics[mkey][0].get("cvssData", {})
            cvss = {
                "version": data.get("version"),
                "base_score": data.get("baseScore"),
                "base_severity": data.get("baseSeverity") or metrics[mkey][0].get("baseSeverity"),
                "vector": data.get("vectorString"),
            }
            break

    weaknesses = []
    for w in cve.get("weaknesses", []):
        for d in w.get("description", []):
            if d.get("value") and d["value"] not in weaknesses:
                weaknesses.append(d["value"])

    refs = [r.get("url") for r in cve.get("references", []) if r.get("url")][:8]

    return {
        "id": cve.get("id"),
        "description": desc_en,
        "cvss": cvss,
        "weaknesses": weaknesses,
        "published": cve.get("published"),
        "last_modified": cve.get("lastModified"),
        "status": cve.get("vulnStatus"),
        "references": refs,
    }


async def _fetch(params: dict[str, Any]) -> dict[str, Any]:
    last_exc: Exception | None = None
    client = await _get_client()
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.get(NVD_URL, params=params)
        except httpx.TimeoutException as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            last_exc = exc
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="CVE introuvable dans NVD.")
            # 403/429/503/autres -> réessayable
            last_exc = httpx.HTTPStatusError(
                f"NVD HTTP {resp.status_code}", request=resp.request, response=resp)
        if attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_BACKOFF_BASE * (2**attempt))

    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Délai dépassé en interrogeant NVD.")
    if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code in (403, 429):
        raise HTTPException(status_code=503, detail="NVD : quota de débit dépassé, réessayer plus tard.")
    raise HTTPException(status_code=502, detail="Service NVD indisponible.")


async def lookup_cve(cve_id: str | None, keyword: str | None, limit: int) -> dict[str, Any]:
    cid = (cve_id or "").strip().upper() or None
    kw = (keyword or "").strip() or None
    if not cid and not kw:
        raise HTTPException(status_code=400, detail="Fournir 'cve_id' (ex. CVE-2021-44228) ou 'keyword'.")
    if cid and not _CVE_RE.match(cid):
        raise HTTPException(status_code=400, detail="'cve_id' mal formé (attendu CVE-AAAA-NNNN).")
    if not (1 <= limit <= 20):
        raise HTTPException(status_code=400, detail="'limit' attendu dans [1, 20].")

    key = f"{cid}|{kw}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    if cid:
        params = {"cveId": cid}
    else:
        params = {"keywordSearch": kw, "resultsPerPage": limit}

    payload = await _fetch(params)
    vulns = payload.get("vulnerabilities", [])
    if cid and not vulns:
        raise HTTPException(status_code=404, detail="CVE introuvable dans NVD.")

    shaped = {
        "query": {"cve_id": cid, "keyword": kw},
        "total_results": payload.get("totalResults"),
        "count": min(len(vulns), limit),
        "cves": [_shape(v) for v in vulns[:limit]],
        "source": SOURCE_NAME,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _cache[key] = (time.time(), shaped)
    return {**shaped, "cached": False}


@router.get("/cve/lookup")
async def cve_lookup(
    cve_id: str | None = Query(None, description="Exact CVE id, e.g. 'CVE-2021-44228'"),
    keyword: str | None = Query(None, description="Keyword / product search, e.g. 'log4j'"),
    limit: int = Query(10, description="Max results for keyword search [1-20], e.g. 10"),
) -> JSONResponse:
    """GET /cve/lookup?cve_id=  OR  ?keyword= — recherche de vulnérabilités CVE (NVD)."""
    data = await lookup_cve(cve_id, keyword, limit)
    return JSONResponse(content=data)


@router.get("/cve/health")
async def cve_health() -> JSONResponse:
    upstream_ok = False
    detail = "unreachable"
    try:
        client = await _get_client()
        r = await client.get(NVD_URL, params={"cveId": "CVE-2021-44228"},
                             timeout=httpx.Timeout(12.0, connect=4.0))
        upstream_ok = r.status_code == 200
        detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    return JSONResponse(
        status_code=200 if upstream_ok else 503,
        content={
            "endpoint": "cve",
            "status": "ok" if upstream_ok else "degraded",
            "upstream": {"source": SOURCE_NAME, "reachable": upstream_ok, "detail": detail},
            "api_key": bool(NVD_API_KEY),
            "cache_entries": len(_cache),
        },
    )
