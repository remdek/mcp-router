"""Search registered downstream MCP servers."""

from __future__ import annotations

from typing import Any

from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore


def search_servers(
    server_manager: ServerManager,
    store: SqliteStore,
    query: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    store.connect()
    tool_counts: dict[str, int] = {}
    for tool in store.list_all_tools():
        server = tool["server_name"]
        tool_counts[server] = tool_counts.get(server, 0) + 1

    q = query.lower().strip()
    results: list[dict[str, Any]] = []
    for server in server_manager.list_servers():
        cfg = server_manager.get_server_config(server.name)
        groups = list(cfg.groups) if cfg else []
        haystack = " ".join([server.name, server.description, server.type, *groups]).lower()
        if q and not any(token in haystack for token in q.split() if len(token) > 1):
            continue
        results.append(
            {
                "server_name": server.name,
                "type": server.type,
                "description": server.description,
                "groups": groups,
                "indexed_tool_count": tool_counts.get(server.name, 0),
                "index_enabled": bool(cfg.index) if cfg else True,
            }
        )

    if q:
        results.sort(
            key=lambda s: (
                q in s["server_name"].lower(),
                q in s["description"].lower(),
                s["indexed_tool_count"],
            ),
            reverse=True,
        )
    else:
        results.sort(key=lambda s: s["server_name"])
    return results[:limit]
