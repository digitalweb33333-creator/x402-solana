"""Collecte best-effort de signaux publics (keyless) pour les endpoints premium.

Réutilise le canal Jina r.jina.ai → DuckDuckGo de web_extract (gratuit, sans clé, la
requête part de l'IP de Jina). Sert à GROUNDER la synthèse LLM de #1 due-diligence et
#2 analysis-report sur de vraies sources publiques.

« Jamais d'erreur après paiement » : ces helpers ne lèvent JAMAIS. Ils renvoient
(résultats, markdown, None) ou ([], None, "raison"). L'endpoint dégrade alors proprement.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, unquote
from typing import Any

from app.sources.http_util import client, get_json

_JINA_HEADERS = {"Accept": "application/json", "X-Return-Format": "markdown"}


def _trim(text: str | None, n: int) -> str | None:
    if not text:
        return text
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …[truncated]"


async def search(query: str, *, limit: int = 6, content_chars: int = 4000
                 ) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Recherche web keyless. Renvoie (results[{title,url}], markdown, error). Ne lève jamais."""
    try:
        c = await client("jina", timeout=30.0, connect=6.0)
        ddg = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        data, err = await get_json(c, f"https://r.jina.ai/{ddg}", headers=_JINA_HEADERS, attempts=2)
        if err or not isinstance(data, dict):
            return [], None, err or "bad_response"
        d = data.get("data") or {}
        content = d.get("content") or ""
        links = d.get("links") or {}

        candidates: list[tuple[str, str]] = []
        if isinstance(links, dict):
            candidates += [(str(t), u) for t, u in links.items() if isinstance(u, str)]
        candidates += [(t, u) for t, u in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", content)]

        results, seen = [], set()
        for text, url in candidates:
            m = re.search(r"[?&]uddg=([^&]+)", url)
            if m:
                url = unquote(m.group(1))
            if not url.startswith("http"):
                continue
            host = url.split("/")[2] if len(url.split("/")) > 2 else ""
            if any(b in host for b in ("duckduckgo.com", "jina.ai", "bing.com")) or url in seen:
                continue
            seen.add(url)
            results.append({"title": _trim((text or host).strip(), 160), "url": url})
            if len(results) >= limit:
                break
        return results, _trim(content, content_chars), None
    except Exception as exc:  # best-effort : jamais de propagation
        return [], None, type(exc).__name__


async def gather(queries: list[str], *, per_query: int = 5, content_chars: int = 3500
                 ) -> dict[str, Any]:
    """Lance plusieurs recherches EN PARALLÈLE (jamais en série) et fusionne les signaux.

    Renvoie {results, markdown_blocks, sources_ok, any_ok}.
    """
    tasks = [search(q, limit=per_query, content_chars=content_chars) for q in queries]
    settled = await asyncio.gather(*tasks, return_exceptions=True)

    merged, seen, blocks, oks = [], set(), [], []
    for q, res in zip(queries, settled):
        if isinstance(res, Exception):
            oks.append({"query": q, "ok": False, "error": type(res).__name__})
            continue
        results, markdown, err = res
        oks.append({"query": q, "ok": err is None, "error": err, "result_count": len(results)})
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                merged.append(r)
        if markdown:
            blocks.append({"query": q, "markdown": markdown})
    return {"results": merged, "markdown_blocks": blocks, "sources_ok": oks,
            "any_ok": any(o["ok"] for o in oks)}
