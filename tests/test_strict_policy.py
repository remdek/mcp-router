"""Tests for optional strict / dual MCP surface policy."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_router.services.responses import remediation_error
from mcp_router.services.strict_policy import (
    StrictPolicy,
    build_strict_policy,
    get_surface,
    is_read_only_tool,
    set_surface,
)


def test_default_surface_is_default() -> None:
    set_surface("default")
    assert get_surface() == "default"


def test_empty_policy_denies_on_strict() -> None:
    policy = StrictPolicy()
    ok, reason = policy.allows_tool("hindsight", "recall")
    assert ok is False
    assert "empty allow-list" in reason


def test_server_allowlist_allows_any_tool_on_server() -> None:
    policy = StrictPolicy(servers=frozenset({"hindsight"}))
    ok, _ = policy.allows_tool("hindsight", "recall")
    assert ok is True
    ok2, reason = policy.allows_tool("playwright", "browser_click")
    assert ok2 is False
    assert "outside STRICT_ALLOWLIST" in reason


def test_tool_allowlist() -> None:
    policy = StrictPolicy(tools=frozenset({"hindsight:recall"}))
    assert policy.allows_tool("hindsight", "recall")[0] is True
    assert policy.allows_tool("hindsight", "retain")[0] is False


def test_read_only_requires_annotation() -> None:
    policy = StrictPolicy(servers=frozenset({"hindsight"}), read_only=True)
    assert (
        policy.allows_tool(
            "hindsight",
            "recall",
            tool_schema={"annotations": {"readOnlyHint": True}},
        )[0]
        is True
    )
    ok, reason = policy.allows_tool(
        "hindsight",
        "retain",
        tool_schema={"annotations": {"readOnlyHint": False}},
    )
    assert ok is False
    assert "read-only" in reason


def test_read_only_only_mode_without_names() -> None:
    policy = StrictPolicy(read_only=True)
    assert (
        policy.allows_tool(
            "any",
            "get",
            tool_schema={"annotations": {"readOnlyHint": True}},
        )[0]
        is True
    )
    assert policy.allows_tool("any", "write", tool_schema={})[0] is False


def test_is_read_only_tool() -> None:
    assert is_read_only_tool({"annotations": {"readOnlyHint": True}}) is True
    assert is_read_only_tool({"annotations": {"readOnlyHint": False}}) is False
    assert is_read_only_tool({}) is False


def test_filter_tool_results() -> None:
    policy = StrictPolicy(tools=frozenset({"a:t1"}))
    results = [
        {"server_name": "a", "tool_name": "t1", "tool_schema": {}},
        {"server_name": "a", "tool_name": "t2", "tool_schema": {}},
        {"server_name": "b", "tool_name": "t1", "tool_schema": {}},
    ]
    filtered = policy.filter_tool_results(results)
    assert len(filtered) == 1
    assert filtered[0]["tool_name"] == "t1"


def test_build_from_csv_env() -> None:
    policy = build_strict_policy(allowlist="hindsight,searxng:searxng_web_search")
    assert "hindsight" in policy.servers
    assert "searxng:searxng_web_search" in policy.tools


def test_build_from_json_env() -> None:
    policy = build_strict_policy(
        allowlist='{"servers":["hindsight"],"tools":["graphiti:search_nodes"],"read_only":true}'
    )
    assert policy.servers == frozenset({"hindsight"})
    assert policy.tools == frozenset({"graphiti:search_nodes"})
    assert policy.read_only is True


def test_build_from_registry_when_env_empty() -> None:
    policy = build_strict_policy(
        allowlist="",
        registry_profile={"servers": ["context7"], "readOnly": False},
    )
    assert policy.servers == frozenset({"context7"})


def test_env_overrides_registry() -> None:
    policy = build_strict_policy(
        allowlist="hindsight",
        registry_profile={"servers": ["context7"]},
    )
    assert policy.servers == frozenset({"hindsight"})


def test_build_from_file(tmp_path: Path) -> None:
    path = tmp_path / "strict.json"
    path.write_text(json.dumps({"servers": ["searxng"], "tools": []}), encoding="utf-8")
    policy = build_strict_policy(allowlist=str(path))
    assert policy.servers == frozenset({"searxng"})


def test_strict_denied_remediation_shape() -> None:
    payload = remediation_error(
        "strict_denied",
        error="outside allow-list",
        server_name="playwright",
        tool_name="browser_click",
    )
    assert payload["ok"] is False
    assert payload["failure_kind"] == "strict_denied"
    assert payload["error_source"] == "router"
    assert "/mcp/v2" in payload["reconnect_hint"] or "strict" in payload["reconnect_hint"].lower()
    assert payload["next_action"]


async def test_dual_surface_middleware_rewrites_path() -> None:
    from mcp_router.http_server import DualMcpSurfaceMiddleware

    seen: dict[str, str] = {}

    async def inner(scope: dict, receive: object, send: object) -> None:
        del receive, send
        seen["path"] = scope["path"]
        seen["surface"] = get_surface()

    app = DualMcpSurfaceMiddleware(inner, enabled=True)
    await app({"type": "http", "path": "/mcp/v2", "raw_path": b"/mcp/v2"}, None, None)
    assert seen["path"] == "/mcp"
    assert seen["surface"] == "strict"

    await app({"type": "http", "path": "/mcp", "raw_path": b"/mcp"}, None, None)
    assert seen["path"] == "/mcp"
    assert seen["surface"] == "default"


def test_default_surface_ignores_empty_policy_for_gate_decision() -> None:
    """Documented contract: enforcement only when surface == strict."""
    set_surface("default")
    policy = StrictPolicy()
    # Callers on /mcp must not consult policy; this asserts policy stays deny-by-default
    # while surface flag is what http_server checks.
    assert get_surface() == "default"
    assert policy.allows_tool("x", "y")[0] is False
