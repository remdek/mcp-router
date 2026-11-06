"""Tests for schema-on-demand / strong-match floor (WOR-1537)."""

from __future__ import annotations

import json

from mcp_router.services.schema_budget import (
    apply_schema_on_demand,
    estimate_results_chars,
    strong_match_floor,
)


def _hit(name: str, score: float, *, schema: dict | None = None) -> dict:
    return {
        "tool_id": f"srv:{name}",
        "server_name": "srv",
        "tool_name": name,
        "description": f"desc {name}",
        "score": score,
        "tool_schema": schema
        if schema is not None
        else {"name": name, "inputSchema": {"type": "object"}},
    }


def test_strong_match_floor_relative_to_top() -> None:
    results = [_hit("a", 0.04), _hit("b", 0.03)]
    floor = strong_match_floor(results, ratio=0.85)
    assert floor is not None
    assert abs(floor - 0.034) < 1e-9


def test_top_hit_keeps_schema_weak_omitted() -> None:
    results = [
        _hit("top", 0.04),
        _hit("mid", 0.033),  # 0.033 / 0.04 = 0.825 < 0.85 → weak
        _hit("low", 0.02),
    ]
    gated = apply_schema_on_demand(results, ratio=0.85)
    assert gated[0]["schema_omitted"] is False
    assert "tool_schema" in gated[0]
    assert gated[1]["schema_omitted"] is True
    assert "tool_schema" not in gated[1]
    assert gated[2]["schema_omitted"] is True
    assert "tool_schema" not in gated[2]
    # Ranking / identity unchanged (top-1 recall).
    assert [r["tool_name"] for r in gated] == ["top", "mid", "low"]


def test_near_top_keeps_schema() -> None:
    results = [_hit("a", 0.04), _hit("b", 0.036)]  # 0.9 of top
    gated = apply_schema_on_demand(results, ratio=0.85)
    assert all(r["schema_omitted"] is False for r in gated)
    assert all("tool_schema" in r for r in gated)


def test_include_schema_for_forces_weak_hit() -> None:
    results = [_hit("top", 0.04), _hit("need", 0.01)]
    gated = apply_schema_on_demand(
        results,
        ratio=0.85,
        include_schema_for=["srv:need"],
    )
    assert gated[1]["schema_omitted"] is False
    assert gated[1]["tool_schema"]["name"] == "need"


def test_include_schemas_keeps_all() -> None:
    results = [_hit("a", 0.04), _hit("b", 0.01)]
    gated = apply_schema_on_demand(results, include_schemas=True)
    assert all("tool_schema" in r for r in gated)


def test_disabled_keeps_all_schemas() -> None:
    results = [_hit("a", 0.04), _hit("b", 0.01)]
    gated = apply_schema_on_demand(results, enabled=False)
    assert all("tool_schema" in r for r in gated)


def test_payload_noticeably_smaller() -> None:
    """Acceptance: typical multi-hit page shrinks when schemas are gated."""
    fat_schema = {
        "name": "x",
        "description": "long " * 40,
        "inputSchema": {
            "type": "object",
            "properties": {f"p{i}": {"type": "string", "description": "d" * 30} for i in range(20)},
        },
    }
    results = [_hit(f"t{i}", 0.04 - i * 0.005, schema=fat_schema) for i in range(5)]
    before = estimate_results_chars(results)
    after = estimate_results_chars(apply_schema_on_demand(results, ratio=0.85))
    assert after < before * 0.55
    # Top-1 still has schema for call preparation.
    gated = apply_schema_on_demand(results, ratio=0.85)
    assert "tool_schema" in gated[0]
    # Round-trip JSON still valid.
    json.loads(json.dumps(gated))
