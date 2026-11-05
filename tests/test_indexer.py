"""Tests for tool indexing pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_router.index.indexer import ToolIndexer
from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore

RAW_TOOLS = [
    {
        "name": "create_task",
        "description": "Create a task",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "Task title"}},
        },
    },
    {
        "name": "list_tasks",
        "description": "List all tasks",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@pytest.fixture
def manager_with_tasks() -> ServerManager:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "tasks": McpServerConfig(
                    name="tasks",
                    type="http",
                    url="http://mock/tasks",
                    description="Task server",
                )
            }
        )
    )
    manager.get_server_tools = AsyncMock(return_value=RAW_TOOLS)  # type: ignore[method-assign]
    manager.disconnect_server = AsyncMock()  # type: ignore[method-assign]
    return manager


async def test_rebuild_index_keyword_only(
    tmp_db: SqliteStore, manager_with_tasks: ServerManager
) -> None:
    indexer = ToolIndexer(tmp_db, manager_with_tasks, embedding_client=None)
    status = await indexer.rebuild_index()

    assert status["ok"] is True
    assert status["tool_count"] == 2
    assert status["mode"] == "keyword_only"
    assert tmp_db.tool_count() == 2
    assert tmp_db.get_tool("tasks", "create_task") is not None


async def test_rebuild_index_with_embeddings(
    tmp_db: SqliteStore, manager_with_tasks: ServerManager
) -> None:
    embedding = MagicMock()
    embedding.embed_texts = AsyncMock(
        side_effect=lambda batch: [[float(i)] * 3 for i in range(len(batch))]
    )
    indexer = ToolIndexer(tmp_db, manager_with_tasks, embedding_client=embedding)
    status = await indexer.rebuild_index()

    assert status["ok"] is True
    assert status["embedding_ok"] is True
    assert status["mode"] == "hybrid"
    assert tmp_db.tool_count() == 2


async def test_rebuild_index_continues_without_embeddings(
    tmp_db: SqliteStore, manager_with_tasks: ServerManager
) -> None:
    embedding = MagicMock()
    embedding.embed_texts = AsyncMock(side_effect=RuntimeError("embedding down"))
    indexer = ToolIndexer(tmp_db, manager_with_tasks, embedding_client=embedding)
    status = await indexer.rebuild_index()

    assert status["ok"] is True
    assert status["embedding_ok"] is False
    assert status["mode"] == "keyword_only"
    assert tmp_db.tool_count() == 2
