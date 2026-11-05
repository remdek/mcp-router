"""Tests for SQLite index storage."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from mcp_router.storage.sqlite_store import SqliteStore
from tests.conftest import SAMPLE_TOOLS


def test_upsert_and_count(populated_store: SqliteStore) -> None:
    assert populated_store.tool_count() == 3
    assert populated_store.get_meta("last_indexed_at") is not None


def test_get_tool(populated_store: SqliteStore) -> None:
    tool = populated_store.get_tool("tasks", "create_task")
    assert tool is not None
    assert tool["tool_name"] == "create_task"
    assert populated_store.get_tool("missing", "tool") is None


def test_clear_index(tmp_db: SqliteStore) -> None:
    tmp_db.upsert_tools(SAMPLE_TOOLS)
    assert tmp_db.tool_count() == 3
    tmp_db.clear_index()
    assert tmp_db.tool_count() == 0


def test_fts_search(populated_store: SqliteStore) -> None:
    results = populated_store.search_fts("create task", limit=5)
    assert results
    assert results[0]["tool_name"] == "create_task"


def test_fts_query_with_hyphen(populated_store: SqliteStore) -> None:
    results = populated_store.search_fts("query-docs", limit=5)
    assert any(r["tool_name"] == "query-docs" for r in results)


def test_vector_search(populated_store: SqliteStore) -> None:
    results = populated_store.search_vector([1.0, 0.0, 0.0], limit=2)
    assert results
    assert results[0]["tool_name"] == "create_task"


def test_brute_force_vector_search_capped(
    tmp_db: SqliteStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Brute-force path must not scan unbounded tool counts (A6)."""
    import logging
    import time

    import mcp_router.storage.sqlite_store as store_mod

    scan_cap = 50
    monkeypatch.setattr(store_mod, "BRUTE_FORCE_VECTOR_SCAN_LIMIT", scan_cap)
    tmp_db._vec_enabled = False  # force brute-force path

    n_tools = scan_cap + 100
    tools = [
        {
            "tool_id": f"bulk:tool_{i:04d}",
            "server_name": "bulk",
            "tool_name": f"tool_{i:04d}",
            "description": f"bulk tool {i}",
            "text_content": f"bulk tool {i}",
            "parameters": [],
            "tool_schema": {"name": f"tool_{i:04d}"},
        }
        for i in range(n_tools)
    ]
    # First tool matches query; rest are orthogonal so ranking is stable.
    embeddings = [[1.0, 0.0, 0.0]] + [[0.0, 1.0, 0.0]] * (n_tools - 1)
    tmp_db.upsert_tools(tools, embeddings)

    with caplog.at_level(logging.WARNING, logger="mcp_router.storage.sqlite_store"):
        t0 = time.perf_counter()
        results = tmp_db.search_vector([1.0, 0.0, 0.0], limit=5)
        elapsed = time.perf_counter() - t0

    assert results
    assert results[0]["tool_name"] == "tool_0000"
    assert elapsed < 2.0, f"brute-force search too slow: {elapsed:.3f}s"
    assert any("brute-force vector search capped" in r.message for r in caplog.records)


def test_list_all_tools(populated_store: SqliteStore) -> None:
    tools = populated_store.list_all_tools()
    assert len(tools) == 3
    names = {t["tool_name"] for t in tools}
    assert names == {"create_task", "query-docs", "find_code"}


@pytest.mark.asyncio
async def test_concurrent_store_access_no_programming_error(
    populated_store: SqliteStore,
) -> None:
    """Stress shared connection: parallel search/status/feedback-style ops.

    Mimics concurrent /mcp search, /status reads, and feedback writes without
    raising sqlite3.ProgrammingError from cross-thread connection use.
    """
    store = populated_store
    n = 40

    def _status_like() -> int:
        return store.tool_count() + len(store.list_all_tools())

    def _search_like(i: int) -> int:
        return len(store.search_fts("task" if i % 2 == 0 else "docs", limit=5))

    def _feedback_like(i: int) -> None:
        now = datetime.now(UTC).isoformat()
        store.insert_feedback(
            {
                "id": str(uuid.uuid4()),
                "created_at": now,
                "caller_id": f"stress-{i}",
                "category": "other",
                "message": f"stress {i}",
                "call_ids": [],
                "search_ids": [],
                "verified_call_ids": [],
                "verified_search_ids": [],
            }
        )
        store.insert_search_log(
            {
                "id": str(uuid.uuid4()),
                "created_at": now,
                "kind": "tools",
                "query": f"q-{i}",
                "results_json": [],
                "caller_id": f"stress-{i}",
                "latency_ms": 1.0,
            }
        )

    async def worker(i: int) -> None:
        await asyncio.gather(
            asyncio.to_thread(_status_like),
            asyncio.to_thread(_search_like, i),
            asyncio.to_thread(_feedback_like, i),
        )

    await asyncio.gather(*[worker(i) for i in range(n)])
    assert store.tool_count() == 3
