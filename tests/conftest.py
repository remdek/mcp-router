"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_router.models.registry import GroupConfig, McpServerConfig, Registry
from mcp_router.storage.sqlite_store import SqliteStore

SAMPLE_TOOLS = [
    {
        "tool_id": "tasks:create_task",
        "server_name": "tasks",
        "tool_name": "create_task",
        "description": "Create a new task in the project",
        "text_content": "create task project management tasks create_task",
        "parameters": [],
        "tool_schema": {"name": "create_task", "description": "Create a new task"},
    },
    {
        "tool_id": "docs:query-docs",
        "server_name": "docs",
        "tool_name": "query-docs",
        "description": "Query library documentation",
        "text_content": "documentation library query docs query-docs",
        "parameters": [],
        "tool_schema": {"name": "query-docs", "description": "Query library documentation"},
    },
    {
        "tool_id": "code:find_code",
        "server_name": "code",
        "tool_name": "find_code",
        "description": "Search code in the repository",
        "text_content": "search code repository find_code",
        "parameters": [],
        "tool_schema": {"name": "find_code", "description": "Search code"},
    },
]

SAMPLE_EMBEDDINGS = [
    [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
]


@pytest.fixture
def tmp_db(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "tools.db")
    store.connect()
    yield store
    store.close()


@pytest.fixture
def populated_store(tmp_db: SqliteStore) -> SqliteStore:
    tmp_db.upsert_tools(SAMPLE_TOOLS, SAMPLE_EMBEDDINGS)
    return tmp_db


@pytest.fixture
def sample_registry() -> Registry:
    return Registry(
        groups=[
            GroupConfig(
                id="tasks",
                description="Task management tools",
                keywords=["задача", "task", "create"],
            )
        ],
        mcp_servers={
            "stdio-server": McpServerConfig(
                name="stdio-server",
                command="python3",
                args=["tests/fixtures/mock_stdio_server.py"],
                description="Local stdio MCP",
            ),
            "http-server": McpServerConfig(
                name="http-server",
                type="http",
                url="http://mock.example/mcp",
                description="HTTP MCP server",
            ),
            "sse-server": McpServerConfig(
                name="sse-server",
                type="sse",
                url="http://mock.example/sse",
                description="SSE MCP server",
            ),
            "ws-server": McpServerConfig(
                name="ws-server",
                type="websocket",
                url="ws://mock.example/ws",
                description="WebSocket MCP server",
            ),
        },
    )
