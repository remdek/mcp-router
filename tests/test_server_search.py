"""Tests for server search."""

from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.server_manager import ServerManager
from mcp_router.services.server_search import search_servers
from mcp_router.storage.sqlite_store import SqliteStore


def test_search_servers_by_query(tmp_db: SqliteStore) -> None:
    tmp_db.upsert_tools(
        [
            {
                "tool_id": "docs:query",
                "server_name": "docs",
                "tool_name": "query",
                "description": "Query docs",
                "text_content": "docs",
                "parameters": [],
                "tool_schema": {},
            }
        ]
    )
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "docs": McpServerConfig(
                    name="docs",
                    type="http",
                    url="http://mock",
                    description="Documentation server",
                    groups=["research"],
                ),
                "github": McpServerConfig(
                    name="github",
                    command="npx",
                    description="GitHub integration",
                    groups=["vcs"],
                ),
            }
        )
    )
    results = search_servers(manager, tmp_db, query="documentation")
    assert len(results) == 1
    assert results[0]["server_name"] == "docs"
    assert results[0]["indexed_tool_count"] == 1
