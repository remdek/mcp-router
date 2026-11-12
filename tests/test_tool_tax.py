"""Tests for tool-tax estimate (WOR-1613 / ADR 0001 companion)."""

from __future__ import annotations

import json

from mcp_router.services.tool_tax import (
    ROUTER_SURFACE_TOOLS,
    compute_tool_tax,
    definition_chars,
    estimate_tokens,
    format_tax_markdown,
    rejected_property_names,
)


def _row(
    server: str,
    tool: str,
    *,
    description: str = "d",
    input_schema: dict | None = None,
    fat: bool = False,
) -> dict:
    schema = {
        "name": tool,
        "description": description,
        "inputSchema": input_schema
        if input_schema is not None
        else (
            {
                "type": "object",
                "properties": {
                    f"p{i}": {"type": "string", "description": "x" * 40} for i in range(30)
                },
            }
            if fat
            else {"type": "object", "properties": {"q": {"type": "string"}}}
        ),
    }
    return {
        "tool_id": f"{server}:{tool}",
        "server_name": server,
        "tool_name": tool,
        "description": description,
        "tool_schema": schema,
    }


def test_estimate_tokens_tool_lens_fit() -> None:
    # Spot-check the published ToolLens constants (overhead + chars/2.49).
    assert abs(estimate_tokens(1807) - (225 + 1807 / 2.49)) < 1e-9
    assert estimate_tokens(0) == 0.0


def test_rejected_property_names_nested() -> None:
    schema = {
        "properties": {
            "ok": {},
            "EndTime<": {},
            "nested": {"items": {"properties": {"$vectorSearch": {}}}},
        }
    }
    assert rejected_property_names(schema) == ["EndTime<", "$vectorSearch"]


def test_compute_ranks_servers_and_top_tools() -> None:
    tools = [
        _row("light", "a"),
        _row("heavy", "big", fat=True),
        _row("heavy", "mid", fat=True),
        _row(
            "bad",
            "broken",
            input_schema={"type": "object", "properties": {"$bad": {"type": "string"}}},
        ),
    ]
    report = compute_tool_tax(tools, top=2, registry_servers=["heavy", "light", "bad", "ghost"])
    assert report.method == "estimate"
    assert report.tool_count == 3  # rejected excluded
    assert report.servers[0].server_name == "heavy"
    assert report.servers[0].estimated_tokens >= report.servers[-1].estimated_tokens
    assert len(report.top_tools) == 2
    assert report.top_tools[0].server_name == "heavy"
    kinds = {p["kind"] for p in report.problems}
    assert "rejected_properties" in kinds
    assert "not_in_index" in kinds
    ghost = next(p for p in report.problems if p.get("server_name") == "ghost")
    assert ghost["kind"] == "not_in_index"
    assert report.router_tokens > 0
    assert report.catalog_tokens > report.router_tokens
    assert "tokens" in report.how_to_read.lower()


def test_markdown_mentions_how_to_read() -> None:
    report = compute_tool_tax([_row("s", "t")], top=5)
    md = format_tax_markdown(report)
    assert "mcp-router" in md
    assert "Heaviest tools" in md
    assert "how_to_read" not in md  # footnote uses prose, not the key name
    assert "2.49" in md or "estimated" in md.lower()


def test_definition_chars_stable() -> None:
    n = definition_chars("x", "y", {"type": "object"})
    assert n == len(
        json.dumps(
            {"name": "x", "description": "y", "input_schema": {"type": "object"}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def test_router_surface_nonempty() -> None:
    assert len(ROUTER_SURFACE_TOOLS) >= 5
    names = {t["name"] for t in ROUTER_SURFACE_TOOLS}
    assert "search_tools" in names
    assert "call_server_tool" in names


def test_store_list_tools_with_schemas(populated_store) -> None:
    rows = populated_store.list_tools_with_schemas()
    assert len(rows) == 3
    assert all("tool_schema" in r for r in rows)
    report = compute_tool_tax(rows, top=10)
    assert report.tool_count == 3
    assert report.server_count == 3
