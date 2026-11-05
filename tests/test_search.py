"""Tests for hybrid tool search."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_router.index.search import ToolSearch
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore


@pytest.fixture
def server_manager() -> ServerManager:
    return ServerManager()


@pytest.fixture
def search_engine(populated_store: SqliteStore, server_manager: ServerManager) -> ToolSearch:
    return ToolSearch(store=populated_store, server_manager=server_manager)


async def test_fts_search_returns_relevant_tool(search_engine: ToolSearch) -> None:
    results = await search_engine.search("create task", limit=5)
    assert results
    assert results[0]["tool_name"] == "create_task"
    assert results[0]["server_name"] == "tasks"


async def test_search_documentation_query(search_engine: ToolSearch) -> None:
    results = await search_engine.search("library documentation", limit=5)
    assert any(r["tool_name"] == "query-docs" for r in results)


async def test_rrf_merge_prefers_both_channels(
    populated_store: SqliteStore, server_manager: ServerManager
) -> None:
    embedding_client = MagicMock()
    embedding_client.embed_texts = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    engine = ToolSearch(
        store=populated_store,
        server_manager=server_manager,
        embedding_client=embedding_client,
    )
    results = await engine.search("create task", limit=5)
    assert results
    assert results[0]["tool_name"] == "create_task"
    embedding_client.embed_texts.assert_awaited_once()


async def test_keyword_fallback_substring(populated_store: SqliteStore) -> None:
    populated_store.clear_index()
    populated_store.upsert_tools(
        [
            {
                "tool_id": "x:alpha",
                "server_name": "x",
                "tool_name": "alpha",
                "description": "Alpha tool",
                "text_content": "alpha",
                "parameters": [],
                "tool_schema": {},
            }
        ]
    )
    engine = ToolSearch(store=populated_store, server_manager=ServerManager())
    results = await engine.search("alpha", limit=5)
    assert results
    assert results[0]["tool_name"] == "alpha"
