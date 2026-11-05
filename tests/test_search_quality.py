"""Tests for SearchLog, aliases boost, and shadow ranker wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_router.index.extractor import _enrich_text_content
from mcp_router.index.search import ToolSearch
from mcp_router.services.aliases import AliasCatalog, load_aliases
from mcp_router.services.search_telemetry import (
    SearchTelemetry,
    classify_call_outcome,
    sanitize_query,
)
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore


def test_sanitize_query_redacts_secrets() -> None:
    q = sanitize_query("find docs bearer sk-abcdefghijklmnopqrstuvwxyz api_key=secret123")
    assert "sk-" not in q.lower() or "[REDACTED]" in q
    assert "secret123" not in q
    assert "[REDACTED]" in q


def test_alias_catalog_load_and_text(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps(
            {
                "servers": {"tasks": ["задачи", "todo list"]},
                "tools": {"tasks:create_task": ["создать задачу", "new todo"]},
            }
        ),
        encoding="utf-8",
    )
    catalog = load_aliases(path)
    text = catalog.append_to_text("base", "tasks", "create_task")
    assert "создать задачу" in text
    assert "todo list" in text
    servers, tools = catalog.matches("создать задачу urgently")
    assert "tasks:create_task" in tools
    assert not servers


def test_alias_boost_in_text_content() -> None:
    aliases = AliasCatalog()
    aliases.servers = {"tasks": ["управление задачами"]}
    aliases.tools = {"tasks:create_task": ["создать задачу"]}
    manager = ServerManager()
    text = _enrich_text_content(
        "tasks",
        {"name": "create_task", "description": "Create task", "inputSchema": {}},
        manager,
        aliases,
    )
    assert "создать задачу" in text
    assert "управление задачами" in text


async def test_alias_boost_raises_rank(
    populated_store: SqliteStore, server_manager: ServerManager
) -> None:
    aliases = AliasCatalog()
    aliases.tools = {"docs:query-docs": ["library documentation"]}
    engine = ToolSearch(
        store=populated_store,
        server_manager=server_manager,
        aliases=aliases,
        alias_boost=5.0,
    )
    results = await engine.search("library documentation", limit=5)
    assert results
    assert results[0]["tool_id"] == "docs:query-docs"


async def test_shadow_ranker_returns_baseline_only_to_caller(
    populated_store: SqliteStore, server_manager: ServerManager
) -> None:
    embedding_client = MagicMock()
    embedding_client.embed_texts = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    engine = ToolSearch(
        store=populated_store,
        server_manager=server_manager,
        embedding_client=embedding_client,
        shadow_ranker=True,
    )
    baseline, shadow = await engine.search_with_shadow(
        "create task", limit=5, vector_weight=0.9, fulltext_weight=0.1
    )
    assert baseline
    assert shadow is not None
    # Public search() still returns baseline only
    public = await engine.search("create task", limit=5)
    assert [r["tool_id"] for r in public] == [r["tool_id"] for r in baseline]


def test_search_log_and_followup_call(tmp_db: SqliteStore) -> None:
    telemetry = SearchTelemetry(tmp_db, outcome_window_sec=600)
    search_id = telemetry.log_search(
        kind="tools",
        query="create task bearer sk-abcdefghijklmnopqrstuvwxyz",
        results=[
            {
                "tool_id": "tasks:create_task",
                "server_name": "tasks",
                "tool_name": "create_task",
            }
        ],
        latency_ms=12.5,
        caller_id="test-caller",
    )
    assert search_id
    row = tmp_db.latest_search_for_caller("test-caller")
    assert row is not None
    assert "sk-" not in row["query"]
    assert "[REDACTED]" in row["query"]
    assert row["results_json"][0]["tool_id"] == "tasks:create_task"
    # No schemas / args leaked
    assert "tool_schema" not in row["results_json"][0]

    from mcp_router.services.search_telemetry import set_caller_id

    set_caller_id("test-caller")
    outcome = telemetry.log_call_outcome("tasks", "create_task")
    assert outcome == "hit"
    follows = tmp_db.search_followed_by_call(search_id)
    assert follows and follows[0]["tool_name"] == "create_task"

    metrics = tmp_db.search_quality_metrics(days=7)
    assert metrics["tool_searches"] == 1
    assert metrics["with_followup_call"] == 1
    assert metrics["followup_rate"] == 1.0


def test_quality_metrics_ttl_cache(tmp_db: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = SearchTelemetry(tmp_db, quality_metrics_ttl_sec=30.0)
    calls = {"n": 0}
    real = tmp_db.search_quality_metrics

    def counting(*, days: int = 7) -> dict:
        calls["n"] += 1
        return real(days=days)

    monkeypatch.setattr(tmp_db, "search_quality_metrics", counting)
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "mcp_router.services.search_telemetry.time.monotonic",
        lambda: clock["t"],
    )

    first = telemetry.quality_metrics(days=7)
    second = telemetry.quality_metrics(days=7)
    assert first == second
    assert calls["n"] == 1

    clock["t"] += 31.0
    third = telemetry.quality_metrics(days=7)
    assert third == first
    assert calls["n"] == 2


def test_search_miss_recorded(tmp_db: SqliteStore) -> None:
    telemetry = SearchTelemetry(tmp_db)
    from mcp_router.services.search_telemetry import set_caller_id

    set_caller_id("miss-caller")
    telemetry.log_search(
        kind="tools",
        query="docs",
        results=[{"tool_id": "docs:query-docs", "server_name": "docs", "tool_name": "query-docs"}],
        latency_ms=1.0,
        caller_id="miss-caller",
    )
    outcome = telemetry.log_call_outcome("tasks", "create_task")
    assert outcome == "miss"
    assert (
        classify_call_outcome(
            [{"tool_id": "docs:query-docs", "server_name": "docs"}], "docs", "other"
        )
        == "wrong_tool"
    )


@pytest.fixture
def server_manager() -> ServerManager:
    return ServerManager()
