"""Tests for agent-visible failure backoff (WOR-1539)."""

from __future__ import annotations

from mcp_router.services.failure_backoff import (
    FailureBackoffTracker,
    annotate_server_results,
    filter_tool_results,
    truncate_reason,
)
from mcp_router.services.responses import remediation_error


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_truncate_reason_prefers_word_boundary() -> None:
    text = "connection refused while dialing upstream host"
    out = truncate_reason(text, 28)
    assert out.endswith("…")
    assert len(out) <= 28
    assert " " not in out[-2:] or out.endswith("…")


def test_record_describe_and_expire() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, clock=clock)
    tracker.record(
        "demo", "Connection refused to localhost:9009", failure_kind="downstream_unreachable"
    )

    assert tracker.is_active("demo") is True
    assert tracker.age_seconds("demo") == 0
    msg = tracker.describe("demo")
    assert msg is not None
    assert msg.startswith("failed 0s ago:")
    assert "Connection refused" in msg

    clock.advance(12.4)
    assert tracker.age_seconds("demo") == 12
    assert "failed 12s ago:" in (tracker.describe("demo") or "")

    clock.advance(50)  # total ~62.4 > 60
    assert tracker.is_active("demo") is False
    assert tracker.describe("demo") is None
    assert tracker.active_servers() == set()


def test_sanitize_and_bound_agent_reason() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, agent_reason_chars=40, clock=clock)
    tracker.record("demo", "Authorization: Bearer super-secret-token-value-here and more text")
    msg = tracker.describe("demo") or ""
    assert "super-secret" not in msg
    assert "[REDACTED]" in msg
    # "failed Ns ago: " prefix + truncated reason
    reason_part = msg.split(": ", 1)[1]
    assert len(reason_part) <= 40


def test_clear_allows_immediate_retry() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, clock=clock)
    tracker.record("demo", "timeout")
    assert tracker.clear("demo") is True
    assert tracker.describe("demo") is None


def test_disabled_tracker_is_noop() -> None:
    tracker = FailureBackoffTracker(enabled=False)
    tracker.record("demo", "boom")
    assert tracker.describe("demo") is None
    assert tracker.active_servers() == set()


def test_filter_and_annotate_server_results() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, clock=clock)
    tracker.record("broken", "dns failed")

    tools = [
        {"server_name": "ok", "tool_name": "a"},
        {"server_name": "broken", "tool_name": "b"},
    ]
    filtered = filter_tool_results(tools, tracker.active_servers())
    assert [t["tool_name"] for t in filtered] == ["a"]

    servers = [
        {"server_name": "ok", "description": "fine"},
        {"server_name": "broken", "description": "dead"},
    ]
    annotated = annotate_server_results(servers, tracker)
    assert annotated[0].get("failure_backoff") is None
    assert annotated[0].get("day_path") is None
    assert "failed" in annotated[1]["failure_backoff"]
    assert annotated[1]["day_path"] == "backoff"
    assert annotated[1]["healthy"] is False


def test_annotate_keeps_quarantine_skip() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, clock=clock)
    tracker.record("broken", "down")
    rows = [{"server_name": "broken", "day_path": "skip", "healthy": False}]
    annotated = annotate_server_results(rows, tracker)
    assert annotated[0]["day_path"] == "skip"
    assert "failed" in annotated[0]["failure_backoff"]


def test_snapshot_shape() -> None:
    clock = _Clock()
    tracker = FailureBackoffTracker(backoff_sec=60, clock=clock)
    tracker.record("demo", "boom", failure_kind="timeout")
    snap = tracker.snapshot()
    assert snap["enabled"] is True
    assert snap["backoff_sec"] == 60.0
    assert snap["active"] == ["demo"]
    assert snap["servers"]["demo"]["failure_kind"] == "timeout"
    assert "failed" in snap["servers"]["demo"]["message"]


def test_remediation_failure_backoff_kind() -> None:
    payload = remediation_error(
        "failure_backoff",
        error="Server in failure backoff: demo: failed 5s ago: connection refused",
        server_name="demo",
        tool_name="t",
    )
    assert payload["failure_kind"] == "failure_backoff"
    assert payload["error_source"] == "router"
    assert "next_action" in payload
    assert "backoff" in payload["next_action"].lower()
