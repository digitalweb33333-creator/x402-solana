"""Point d'entrée du serveur MCP x402-endpoints.

Usage :
    python -m mcp_server                 # stdio (défaut) — pour Claude Desktop / Cursor
    python -m mcp_server --transport http --host 0.0.0.0 --port 8001   # remote / self-host
    python -m mcp_server --info          # affiche la config (mode, base_url) et sort

Le chargement du .env (clé acheteur, base url) est optionnel et silencieux.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

# Charge le .env du projet si présent (pour X402_BUYER_PRIVATE_KEY / BUYER_PRIVATE_KEY).
with contextlib.suppress(Exception):
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .server import build_server, mode_summary  # noqa: E402


def _run_stdio() -> None:
    import anyio

    from mcp.server.stdio import stdio_server

    server = build_server()

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(_main)


def build_http_app(path: str = "/mcp"):
    """Construit une app Starlette servant le serveur MCP en HTTP streamable.

    Réutilisable pour monter le MCP dans une app FastAPI existante.
    """
    import contextlib as _contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = build_server()
    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True
    )

    async def handle(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @_contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    return Starlette(routes=[Mount(path, app=handle)], lifespan=lifespan)


def _run_http(host: str, port: int, path: str) -> None:
    import uvicorn

    app = build_http_app(path)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description="x402-endpoints MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="stdio (défaut, clients locaux) ou http (remote/self-host)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--path", default="/mcp", help="chemin HTTP du endpoint MCP")
    parser.add_argument("--info", action="store_true", help="affiche la config et sort")
    args = parser.parse_args()

    if args.info:
        print(json.dumps(mode_summary(), indent=2))
        return

    if args.transport == "stdio":
        # stdout est réservé au protocole MCP : on logge sur stderr.
        print(f"[x402-mcp] starting (stdio) — {mode_summary()}", file=sys.stderr)
        _run_stdio()
    else:
        print(f"[x402-mcp] starting (http://{args.host}:{args.port}{args.path}) "
              f"— {mode_summary()}", file=sys.stderr)
        _run_http(args.host, args.port, args.path)


if __name__ == "__main__":
    main()
