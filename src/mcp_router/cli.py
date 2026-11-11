"""CLI entry points for mcp-router."""

import argparse
import asyncio
import json
import logging
import os

import uvicorn

from mcp_router.config import get_settings
from mcp_router.embedding.factory import create_embedding_client
from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.index.indexer import ToolIndexer
from mcp_router.services.cache_service import CacheService
from mcp_router.services.registry_loader import load_registry
from mcp_router.services.server_manager import ServerManager
from mcp_router.services.tool_tax import (
    ROUTER_SURFACE_TOOLS,
    compute_tool_tax,
    count_tokens_anthropic,
    format_tax_markdown,
)
from mcp_router.storage.sqlite_store import SqliteStore

logging.basicConfig(level=logging.INFO)


async def cmd_index_tools() -> int:
    settings = get_settings()
    registry = load_registry(settings.registry_path)

    cache = CacheService()
    manager = ServerManager(cache_service=cache)
    manager.load_registry(registry)

    store = SqliteStore(settings.index_db_path)
    store.connect()

    embedding: EmbeddingProvider | None = None
    if settings.embedding_available:
        embedding = create_embedding_client(settings)

    indexer = ToolIndexer(
        store=store,
        server_manager=manager,
        embedding_client=embedding,
        timeout_sec=settings.index_tools_timeout_sec,
        concurrency=settings.index_concurrency,
    )
    try:
        result = await indexer.rebuild_index()
        print(json.dumps(result, indent=2))
        return 0
    finally:
        if embedding:
            await embedding.close()
        await manager.cleanup_all()
        store.close()


def cmd_serve() -> int:
    settings = get_settings()
    uvicorn.run(
        "mcp_router.http_server:app",
        host=settings.mcp_router_host,
        port=settings.mcp_router_port,
        log_level="info",
        access_log=False,
    )
    return 0


async def cmd_tax(*, top: int, exact: bool, as_json: bool, model: str) -> int:
    """Estimate tool-definition token tax from the existing SQLite index."""
    settings = get_settings()
    store = SqliteStore(settings.index_db_path)
    store.connect()
    try:
        tools = store.list_tools_with_schemas()
        registry_names: list[str] = []
        try:
            registry = load_registry(settings.registry_path)
            registry_names = sorted(registry.mcp_servers.keys())
        except Exception:
            registry_names = []

        catalog_override: float | None = None
        router_override: float | None = None
        method = "estimate"
        if exact:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                print(
                    "error: --exact requires ANTHROPIC_API_KEY "
                    "(sends tool defs only; never env/args)",
                    flush=True,
                )
                return 2
            catalog_override = float(
                await count_tokens_anthropic(tools, api_key=api_key, model=model)
            )
            router_rows = [
                {
                    "tool_id": t["name"],
                    "tool_name": t["name"],
                    "description": t["description"],
                    "tool_schema": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": t["input_schema"],
                    },
                }
                for t in ROUTER_SURFACE_TOOLS
            ]
            router_override = float(
                await count_tokens_anthropic(router_rows, api_key=api_key, model=model)
            )
            method = "count_tokens"

        report = compute_tool_tax(
            tools,
            top=top,
            registry_servers=registry_names,
            catalog_tokens_override=catalog_override,
            router_tokens_override=router_override,
            method=method,
        )
        if as_json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(format_tax_markdown(report), end="")
        return 0
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-router")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Start HTTP MCP server")
    sub.add_parser("index-tools", help="Rebuild tool index")
    tax = sub.add_parser(
        "tax",
        help="Estimate tool-definition token tax from the SQLite index (ADR 0001)",
    )
    tax.add_argument(
        "--top",
        type=int,
        default=20,
        help="How many heaviest tools to list (default 20)",
    )
    tax.add_argument(
        "--exact",
        action="store_true",
        help="Use Anthropic count_tokens (needs ANTHROPIC_API_KEY; sends defs only)",
    )
    tax.add_argument(
        "--model",
        default="claude-sonnet-4-20250514",
        help="Model for --exact count_tokens",
    )
    tax.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON instead of markdown",
    )
    args = parser.parse_args()

    if args.command == "serve":
        raise SystemExit(cmd_serve())
    if args.command == "index-tools":
        raise SystemExit(asyncio.run(cmd_index_tools()))
    if args.command == "tax":
        raise SystemExit(
            asyncio.run(
                cmd_tax(
                    top=args.top,
                    exact=args.exact,
                    as_json=args.as_json,
                    model=args.model,
                )
            )
        )


if __name__ == "__main__":
    main()
