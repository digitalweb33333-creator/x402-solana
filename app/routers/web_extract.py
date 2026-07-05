"""Endpoint — Web search & extract (LLM-ready).

Differentiator: does not return raw HTML but clean MARKDOWN, deduplicated, ready to
drop into a prompt — web search (query) OR extraction of a specific URL. Serves the
"Search" lane (high demand, low supply).

Source (free, keyless, pay-per-call):
- Jina AI Reader/Search: s.jina.ai (search) + r.jina.ai (markdown extraction).
  Free without a key (moderate rate-limit); no fixed subscription.

"computed" tier $0.05. TTL 10 min.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.sources.http_util import TTLCache, client, get_json, utc_now

router = APIRouter()

SOURCE = "Jina AI Reader (r.jina.ai) + Search (s.jina.ai)"
_cache = TTLCache(600)
_JINA_HEADERS = {"Accept": "application/json", "X-Return-Format": "markdown"}


def _trim(text: str | None, n: int) -> str | None:
    if not text:
        return text
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …[truncated]"


async def _search(q: str, limit: int) -> dict[str, Any]:
    # s.jina.ai requires a key (401). We go through r.jina.ai (keyless) which READS
    # a DuckDuckGo results page server-side at Jina -> LLM-ready markdown + links.
    # Benefit: the request leaves from Jina's IP, not ours (anti IP-block).
    c = await client("jina", timeout=30.0, connect=6.0)
    ddg = f"https://html.duckduckgo.com/html/?q={quote(q)}"
    data, err = await get_json(c, f"https://r.jina.ai/{ddg}", headers=_JINA_HEADERS, attempts=2)
    if err or not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"Search source unreachable ({err}); not charged.")
    d = data.get("data") or {}
    content = d.get("content") or ""
    links = d.get("links") or {}

    # 1) candidates = values of Jina's links dict + markdown links in the content
    candidates: list[tuple[str, str]] = []
    if isinstance(links, dict):
        candidates += [(str(t), u) for t, u in links.items() if isinstance(u, str)]
    candidates += [(t, u) for t, u in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", content)]

    results, seen = [], set()
    for text, url in candidates:
        # DuckDuckGo wraps real links in a ?uddg=<urlencoded> redirect
        m = re.search(r"[?&]uddg=([^&]+)", url)
        if m:
            url = unquote(m.group(1))
        if not url.startswith("http"):
            continue
        host = url.split("/")[2] if len(url.split("/")) > 2 else ""
        if any(b in host for b in ("duckduckgo.com", "jina.ai", "bing.com")) or url in seen:
            continue
        seen.add(url)
        results.append({"title": _trim(text.strip() or host, 160), "url": url})
        if len(results) >= limit:
            break
    if not results and not content:
        raise HTTPException(status_code=502, detail="Search returned no parseable results; not charged.")
    return {"mode": "search", "query": q, "count": len(results),
            "results": results, "results_markdown": _trim(content, 8000) if not results else None}


async def _extract(url: str) -> dict[str, Any]:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="'url' must start with http(s)://")
    c = await client("jina", timeout=30.0, connect=6.0)
    data, err = await get_json(c, f"https://r.jina.ai/{url}", headers=_JINA_HEADERS, attempts=2)
    if err or not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"Extraction source unreachable ({err}); not charged.")
    d = data.get("data") or {}
    content = d.get("content")
    if not content:
        raise HTTPException(status_code=502, detail="No extractable content at that URL; not charged.")
    return {"mode": "extract", "url": url, "title": d.get("title"),
            "markdown": _trim(content, 20000), "char_count": len(content),
            "published_time": d.get("publishedTime")}


async def run(query: str | None, url: str | None, limit: int) -> dict[str, Any]:
    q = (query or "").strip() or None
    u = (url or "").strip() or None
    if not q and not u:
        raise HTTPException(status_code=400, detail="Provide 'query' (web search) or 'url' (extract a page).")
    if not (1 <= limit <= 10):
        raise HTTPException(status_code=400, detail="'limit' must be in [1, 10].")

    key = f"{q}|{u}|{limit}"
    cached = _cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}

    body = await (_extract(u) if u else _search(q, limit))
    shaped = {**body, "source": SOURCE, "timestamp": utc_now(),
              "disclaimer": "Content fetched live from public web via Jina Reader; may be incomplete or rate-limited."}
    _cache.set(key, shaped)
    return {**shaped, "cached": False}


@router.get("/web/extract")
async def web_extract(
    query: str | None = Query(None, description="Web search query, e.g. 'latest Base chain TVL'"),
    url: str | None = Query(None, description="If set, extract this page as clean markdown instead of searching"),
    limit: int = Query(5, description="Max search results [1-10], e.g. 5"),
) -> JSONResponse:
    """GET /web/extract?query=  OR  ?url= — web search or page extraction returned as clean, LLM-ready markdown."""
    return JSONResponse(content=await run(query, url, limit))


@router.get("/web/extract/health")
async def web_extract_health() -> JSONResponse:
    c = await client("jina", timeout=20.0)
    data, err = await get_json(c, "https://r.jina.ai/https://example.com", headers=_JINA_HEADERS, attempts=1)
    ok = err is None and isinstance(data, dict)
    return JSONResponse(status_code=200 if ok else 503, content={
        "endpoint": "web-extract", "status": "ok" if ok else "degraded",
        "upstream": {"source": SOURCE, "reachable": ok, "detail": err or "HTTP 200"},
        "cache_entries": len(_cache)})
