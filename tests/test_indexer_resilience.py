"""Tests for resilient indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_router.index.indexer import ToolIndexer
from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore

RAW_TOOLS = [
    {
        "name": "create_task",
        "description": "Create a task",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


@pytest.fixture
def manager_with_mixed_servers() -> ServerManager:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "tasks": McpServerConfig(
                    name="tasks",
                    type="http",
                    url="http://mock/tasks",
                    description="Task server",
                ),
                "browsermcp": McpServerConfig(
                    name="browsermcp",
                    command="npx",
                    args=["@browsermcp/mcp@latest"],
                    index=False,
                ),
            }
        )
    )
    manager.get_server_tools = AsyncMock(return_value=RAW_TOOLS)  # type: ignore[method-assign]
    manager.disconnect_server = AsyncMock()  # type: ignore[method-assign]
    return manager


async def test_reindex_skips_index_false_servers(
    tmp_db: SqliteStore, manager_with_mixed_servers: ServerManager
) -> None:
    indexer = ToolIndexer(tmp_db, manager_with_mixed_servers, embedding_client=None)
    status = await indexer.rebuild_index()

    assert status["tool_count"] == 1
    manager_with_mixed_servers.get_server_tools.assert_awaited_once()
    assert manager_with_mixed_servers.get_server_tools.await_args.args[0] == "tasks"


async def test_reindex_keeps_existing_index_when_all_fail(
    populated_store: SqliteStore,
) -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "broken": McpServerConfig(name="broken", command="npx", args=["missing"]),
            }
        )
    )
    manager.get_server_tools = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    manager.disconnect_server = AsyncMock()  # type: ignore[method-assign]

    indexer = ToolIndexer(populated_store, manager, embedding_client=None)
    status = await indexer.rebuild_index()

    assert status["tool_count"] == 3
    assert populated_store.tool_count() == 3
    assert status["failed_servers"] == ["broken"]
