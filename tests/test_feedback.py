"""Tests for feedback/review schema + persistence joins."""

from __future__ import annotations

import pytest

from mcp_router.services.feedback import (
    FeedbackService,
    FeedbackValidationError,
    ReviewTargetNotFound,
)
from mcp_router.services.search_telemetry import SearchTelemetry, set_caller_id
from mcp_router.storage.sqlite_store import SqliteStore


def test_review_joins_call_audit_and_search(tmp_db: SqliteStore) -> None:
    telemetry = SearchTelemetry(tmp_db)
    set_caller_id("agent-a")
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
        latency_ms=5.0,
        caller_id="agent-a",
    )
    assert search_id
    call_id = telemetry.log_call_audit("tasks", "create_task", ok=True)
    assert call_id
    audit = tmp_db.get_call_audit(call_id)
    assert audit is not None
    assert audit["search_id"] == search_id
    assert audit["server_name"] == "tasks"

    svc = FeedbackService(tmp_db)
    received = svc.submit_review(
        usefulness="useful",
        call_id=call_id,
        reason="Solved the task bearer sk-abcdefghijklmnopqrstuvwxyz",
        caller_id="agent-a",
    )
    assert received["status"] == "received"
    assert received["target_kind"] == "call"
    review_row = tmp_db.get_review("call", call_id)
    assert review_row is not None
    assert review_row["usefulness"] == "useful"
    assert "sk-" not in (review_row["reason"] or "")
    assert "[REDACTED]" in (review_row["reason"] or "")

    again = svc.submit_review(usefulness="not_useful", call_id=call_id)
    assert again["status"] == "already_reviewed"
    assert again["review_id"] == received["review_id"]

    search_review = svc.submit_review(
        usefulness="partly",
        search_id=search_id,
        reason="Top hit was right",
    )
    assert search_review["status"] == "received"
    joined = tmp_db.reviews_for_search(search_id)
    kinds = {(r["target_kind"], r["usefulness"]) for r in joined}
    assert ("call", "useful") in kinds
    assert ("search", "partly") in kinds


def test_feedback_verifies_refs_without_rejecting_unknown(tmp_db: SqliteStore) -> None:
    telemetry = SearchTelemetry(tmp_db)
    search_id = telemetry.log_search(
        kind="tools",
        query="docs",
        results=[{"tool_id": "docs:query-docs", "server_name": "docs", "tool_name": "query-docs"}],
        latency_ms=1.0,
        caller_id="fb",
    )
    call_id = telemetry.log_call_audit("docs", "query-docs", caller_id="fb")
    svc = FeedbackService(tmp_db)
    out = svc.submit_feedback(
        category="friction",
        message="Hard to find the right tool api_key=supersecret",
        call_ids=[call_id or "", "missing-call-id"],
        search_ids=[search_id or "", "missing-search"],
    )
    assert out["status"] == "received"
    assert call_id in out["verified_call_ids"]
    assert "missing-call-id" not in out["verified_call_ids"]
    assert search_id in out["verified_search_ids"]
    row = tmp_db.get_feedback(out["feedback_id"])
    assert row is not None
    assert row["category"] == "friction"
    assert "supersecret" not in row["message"]
    assert "[REDACTED]" in row["message"]


def test_review_requires_existing_target(tmp_db: SqliteStore) -> None:
    svc = FeedbackService(tmp_db)
    with pytest.raises(ReviewTargetNotFound):
        svc.submit_review(usefulness="useful", call_id="no-such-call")
    with pytest.raises(FeedbackValidationError):
        svc.submit_review(usefulness="useful")
    with pytest.raises(FeedbackValidationError):
        svc.submit_review(usefulness="useful", call_id="x", search_id="y")
    with pytest.raises(FeedbackValidationError):
        svc.submit_feedback(category="pricing", message="nope")  # type: ignore[arg-type]
    with pytest.raises(FeedbackValidationError):
        svc.submit_feedback(category="quality", message="   ")


def test_call_audit_schema_exists(tmp_db: SqliteStore) -> None:
    assert tmp_db._conn is not None
    tables = {
        r[0]
        for r in tmp_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"call_audit", "reviews", "feedback", "search_log"}.issubset(tables)
