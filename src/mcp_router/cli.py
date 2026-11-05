"""CLI entry points for mcp-router."""

import argparse
import asyncio
import json
import logging

import uvicorn

from mcp_router.config import get_settings
from mcp_router.embedding.factory import create_embedding_client
from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.index.indexer import ToolIndexer
from mcp_router.services.cache_service import CacheService
from mcp_router.services.registry_loader import load_registry
from mcp_router.services.server_manager import ServerManager
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-router")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Start HTTP MCP server")
    sub.add_parser("index-tools", help="Rebuild tool index")
    args = parser.parse_args()

    if args.command == "serve":
        raise SystemExit(cmd_serve())
    if args.command == "index-tools":
        raise SystemExit(asyncio.run(cmd_index_tools()))


if __name__ == "__main__":
    main()
