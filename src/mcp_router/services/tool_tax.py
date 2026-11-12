"""Estimate tool-definition token tax from the SQLite index (WOR-1613).

Companion to ADR 0001 «measure before fattening». Heuristic matches ToolLens
``toollens tax`` (chars / 2.49 + overhead); optional Anthropic ``count_tokens``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

# Fitted to Anthropic count_tokens on ToolLens's 110-server catalog
# (3,558,435 chars → 1,428,973 tokens) and their three meta-tools.
CHARS_PER_TOKEN = 2.49
OVERHEAD_TOKENS = 225.0

# Claude Messages API: property names must match this at any depth.
_PROP_NAME_OK = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

# Thin router surface — approximate Anthropic-shaped defs for the estimate.
# Keep in sync with http_mcp_tools.register_mcp_tools (names + main args).
ROUTER_SURFACE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_router_status",
        "description": "Router health: index state, embedding mode, reindex progress, "
        "downstream probes, reconnect hints.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_servers",
        "description": "Search registered downstream MCP servers (not individual tools).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "search_tools",
        "description": "Search MCP tools by semantic or keyword query (RU/EN).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "vector_weight": {"type": "number", "default": 0.4},
                "fulltext_weight": {"type": "number", "default": 0.6},
                "max_per_server": {"type": ["integer", "null"]},
                "include_schemas": {"type": "boolean", "default": False},
                "include_schema_for": {"type": ["array", "null"], "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_tool_schema",
        "description": "Fetch full tool_schema for one indexed tool (schema-on-demand).",
        "input_schema": {
            "type": "object",
            "properties": {
                "server_name": {"type": "string"},
                "tool_name": {"type": "string"},
            },
            "required": ["server_name", "tool_name"],
        },
    },
    {
        "name": "call_server_tool",
        "description": "Call a tool on a downstream MCP server.",
        "input_schema": {
            "type": "object",
            "properties": {
                "server_name": {"type": "string"},
                "tool_name": {"type": "string"},
                "arguments": {"type": ["object", "null"]},
            },
            "required": ["server_name", "tool_name"],
        },
    },
    {
        "name": "review",
        "description": "Rate a prior search or call after using its result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "usefulness": {
                    "type": "string",
                    "enum": ["useful", "partly", "not_useful", "not_sure"],
                },
                "call_id": {"type": ["string", "null"]},
                "search_id": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["usefulness"],
        },
    },
    {
        "name": "feedback",
        "description": "Report problems or suggestions about mcp-router search/call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["quality", "friction", "other"]},
                "message": {"type": "string"},
                "call_ids": {"type": ["array", "null"], "items": {"type": "string"}},
                "search_ids": {"type": ["array", "null"], "items": {"type": "string"}},
            },
            "required": ["category", "message"],
        },
    },
]


@dataclass
class ToolTaxProp:
    tool_id: str
    server_name: str
    tool_name: str
    chars: int
    estimated_tokens: float
    rejected_properties: list[str] = field(default_factory=list)


@dataclass
class ServerTaxProp:
    server_name: str
    tool_count: int
    chars: int
    estimated_tokens: float


@dataclass
class ToolTaxReport:
    method: str  # "estimate" | "count_tokens"
    tool_count: int
    server_count: int
    catalog_chars: int
    catalog_tokens: float
    router_chars: int
    router_tokens: float
    servers: list[ServerTaxProp]
    top_tools: list[ToolTaxProp]
    problems: list[dict[str, Any]]
    how_to_read: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(chars: int) -> float:
    """Local heuristic used by ToolLens ``toollens tax`` (not API-accurate)."""
    if chars <= 0:
        return 0.0
    return OVERHEAD_TOKENS + chars / CHARS_PER_TOKEN


def definition_chars(name: str, description: str, input_schema: Any) -> int:
    """UTF-8-ish character count of an Anthropic-shaped tool definition JSON."""
    payload = {
        "name": name,
        "description": description or "",
        "input_schema": input_schema if input_schema is not None else {"type": "object"},
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def rejected_property_names(schema: Any) -> list[str]:
    """Property names Claude Messages API refuses (any depth under ``properties``)."""
    bad: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for key, child in props.items():
                if not _PROP_NAME_OK.match(str(key)):
                    bad.append(str(key))
                walk(child)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items)
        elif isinstance(items, list):
            for item in items:
                walk(item)
        for key in ("anyOf", "oneOf", "allOf"):
            branch = node.get(key)
            if isinstance(branch, list):
                for item in branch:
                    walk(item)
        addl = node.get("additionalProperties")
        if isinstance(addl, dict):
            walk(addl)

    walk(schema if isinstance(schema, dict) else {})
    # Stable unique order.
    seen: set[str] = set()
    out: list[str] = []
    for name in bad:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _input_schema_from_row(tool_schema: Any) -> Any:
    if isinstance(tool_schema, str):
        try:
            tool_schema = json.loads(tool_schema)
        except json.JSONDecodeError:
            return {"type": "object"}
    if not isinstance(tool_schema, dict):
        return {"type": "object"}
    if "inputSchema" in tool_schema:
        return tool_schema.get("inputSchema") or {"type": "object"}
    if "input_schema" in tool_schema:
        return tool_schema.get("input_schema") or {"type": "object"}
    # Already a bare JSON Schema object.
    if "type" in tool_schema or "properties" in tool_schema:
        return tool_schema
    return {"type": "object"}


def _tool_name_from_row(row: dict[str, Any], tool_schema: Any) -> str:
    name = str(row.get("tool_name") or "")
    if name:
        return name
    if isinstance(tool_schema, dict):
        return str(tool_schema.get("name") or "unknown")
    return "unknown"


def compute_tool_tax(
    tools: list[dict[str, Any]],
    *,
    top: int = 20,
    registry_servers: list[str] | None = None,
    catalog_tokens_override: float | None = None,
    router_tokens_override: float | None = None,
    method: str = "estimate",
) -> ToolTaxReport:
    """Rank indexed tools/servers by estimated definition cost.

    ``tools`` rows need ``server_name``, ``tool_name`` / ``tool_id``, ``description``,
    and ``tool_schema`` (JSON object or string).
    """
    per_tool: list[ToolTaxProp] = []
    problems: list[dict[str, Any]] = []
    server_chars: dict[str, int] = defaultdict(int)
    server_counts: dict[str, int] = defaultdict(int)

    for row in tools:
        raw_schema = row.get("tool_schema")
        if isinstance(raw_schema, str):
            try:
                parsed_schema: Any = json.loads(raw_schema)
            except json.JSONDecodeError:
                parsed_schema = {}
        else:
            parsed_schema = raw_schema if isinstance(raw_schema, dict) else {}

        server = str(row.get("server_name") or "unknown")
        tool_name = _tool_name_from_row(row, parsed_schema)
        tool_id = str(row.get("tool_id") or f"{server}:{tool_name}")
        description = str(row.get("description") or "")
        if not description and isinstance(parsed_schema, dict):
            description = str(parsed_schema.get("description") or "")

        input_schema = _input_schema_from_row(parsed_schema)
        rejected = rejected_property_names(input_schema)
        chars = definition_chars(tool_name, description, input_schema)
        server_counts[server] += 1
        if rejected:
            problems.append(
                {
                    "kind": "rejected_properties",
                    "tool_id": tool_id,
                    "properties": rejected,
                }
            )
            # ToolLens excludes rejected tools from the token total.
            continue

        est = estimate_tokens(chars)
        per_tool.append(
            ToolTaxProp(
                tool_id=tool_id,
                server_name=server,
                tool_name=tool_name,
                chars=chars,
                estimated_tokens=est,
                rejected_properties=[],
            )
        )
        server_chars[server] += chars

    catalog_chars = sum(t.chars for t in per_tool)
    catalog_tokens = (
        float(catalog_tokens_override)
        if catalog_tokens_override is not None
        else estimate_tokens(catalog_chars)
    )

    router_chars = sum(
        definition_chars(t["name"], t["description"], t["input_schema"])
        for t in ROUTER_SURFACE_TOOLS
    )
    router_tokens = (
        float(router_tokens_override)
        if router_tokens_override is not None
        else estimate_tokens(router_chars)
    )

    # Allocate catalog total by char share (matches ToolLens reporting).
    def share(chars: int) -> float:
        if catalog_chars <= 0:
            return 0.0
        return catalog_tokens * chars / catalog_chars

    servers: list[ServerTaxProp] = []
    for name in sorted(server_counts.keys()):
        c = server_chars.get(name, 0)
        servers.append(
            ServerTaxProp(
                server_name=name,
                tool_count=server_counts[name],
                chars=c,
                estimated_tokens=share(c),
            )
        )
    servers.sort(key=lambda s: (-s.estimated_tokens, s.server_name))

    ranked = sorted(per_tool, key=lambda t: (-t.chars, t.tool_id))
    top_n = max(0, int(top))
    top_tools = [
        ToolTaxProp(
            tool_id=t.tool_id,
            server_name=t.server_name,
            tool_name=t.tool_name,
            chars=t.chars,
            estimated_tokens=share(t.chars),
            rejected_properties=t.rejected_properties,
        )
        for t in ranked[:top_n]
    ]

    if registry_servers:
        indexed = set(server_counts)
        for name in registry_servers:
            if name not in indexed:
                problems.append(
                    {
                        "kind": "not_in_index",
                        "server_name": name,
                        "detail": "Registered but no tools in SQLite index "
                        "(failed index, empty tools/list, or not yet indexed).",
                    }
                )

    how = (
        "Numbers are estimated tokens of tool *definitions* if the agent loaded the "
        "full indexed catalog on every request (chars÷2.49 + 225 overhead; ToolLens fit). "
        "`router_*` is the thin mcp-router surface (~7 tools). "
        "Per-server / top-tool rows are the catalog total split by definition size. "
        "Tools with Claude-incompatible property names are listed under problems and "
        "excluded from the total. Use `--exact` / `exact=true` for Anthropic count_tokens."
    )
    if method == "count_tokens":
        how = (
            "catalog_tokens / router_tokens came from Anthropic count_tokens; "
            "per-server and top-tool rows still allocate that total by local char share. "
            "Rejected property names are excluded from the measured set."
        )

    return ToolTaxReport(
        method=method,
        tool_count=len(per_tool),
        server_count=len(server_counts),
        catalog_chars=catalog_chars,
        catalog_tokens=catalog_tokens,
        router_chars=router_chars,
        router_tokens=router_tokens,
        servers=servers,
        top_tools=top_tools,
        problems=problems,
        how_to_read=how,
    )


def format_tax_markdown(report: ToolTaxReport) -> str:
    """Human-readable markdown (ToolLens-style)."""
    approx = "" if report.method == "count_tokens" else "≈ "
    method_note = (
        "count_tokens"
        if report.method == "count_tokens"
        else "estimated locally; `--exact` counts with the API"
    )
    lines = [
        "# MCP tool tax (indexed catalog)\n",
        f"{report.server_count} servers, {report.tool_count:,} tools: "
        f"**{approx}{report.catalog_tokens:,.0f} tokens** of tool definitions "
        f"with every request if the catalog were dumped ({method_note}).",
        f"Through mcp-router the agent sees {len(ROUTER_SURFACE_TOOLS)} tools: "
        f"**{approx}{report.router_tokens:,.0f} tokens**.\n",
        "| Server | Tools | Tokens per request |",
        "|---|---:|---:|",
    ]
    for s in report.servers:
        lines.append(f"| {s.server_name} | {s.tool_count} | ≈ {s.estimated_tokens:,.0f} |")
    if report.top_tools:
        lines.append("\n## Heaviest tools\n")
        lines.append("| Tool | Tokens |")
        lines.append("|---|---:|")
        for t in report.top_tools:
            lines.append(f"| {t.tool_id} | ≈ {t.estimated_tokens:,.0f} |")
    if report.problems:
        lines.append("\n## Problems\n")
        rejected = [p for p in report.problems if p.get("kind") == "rejected_properties"]
        missing = [p for p in report.problems if p.get("kind") == "not_in_index"]
        if rejected:
            lines.append(
                f"- {len(rejected)} tools use property names the Claude API refuses "
                "(excluded from totals):"
            )
            for p in rejected:
                props = "`, `".join(p.get("properties") or [])
                lines.append(f"  - `{p.get('tool_id')}`: `{props}`")
        if missing:
            lines.append(f"- {len(missing)} registry servers have no indexed tools:")
            for p in missing:
                lines.append(f"  - `{p.get('server_name')}`: {p.get('detail')}")
    lines.append(f"\n_{report.how_to_read}_")
    return "\n".join(lines) + "\n"


async def count_tokens_anthropic(
    tools: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    base_url: str = "https://api.anthropic.com",
) -> int:
    """Return tool-definition token delta via Anthropic Messages ``count_tokens``.

    Sends only tool defs (name/description/input_schema) — never env or args.
    """
    import httpx

    api_tools = []
    for row in tools:
        raw = row.get("tool_schema")
        if isinstance(raw, str):
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = raw if isinstance(raw, dict) else {}
        input_schema = _input_schema_from_row(parsed)
        if rejected_property_names(input_schema):
            continue
        name = _tool_name_from_row(row, parsed)
        desc = str(row.get("description") or "")
        if not desc and isinstance(parsed, dict):
            desc = str(parsed.get("description") or "")
        # Anthropic tool names: [a-zA-Z0-9_-]{1,128}; use tool_id-ish safe name.
        tool_id = str(row.get("tool_id") or name)
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id)[:128] or "tool"
        api_tools.append({"name": safe, "description": desc, "input_schema": input_schema})

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    messages = [{"role": "user", "content": "ping"}]
    system = "x"

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=120.0) as client:
        with_tools = await client.post(
            "/v1/messages/count_tokens",
            headers=headers,
            json={
                "model": model,
                "system": system,
                "messages": messages,
                "tools": api_tools,
            },
        )
        with_tools.raise_for_status()
        without = await client.post(
            "/v1/messages/count_tokens",
            headers=headers,
            json={"model": model, "system": system, "messages": messages},
        )
        without.raise_for_status()

    with_n = int(with_tools.json().get("input_tokens") or 0)
    without_n = int(without.json().get("input_tokens") or 0)
    return max(0, with_n - without_n)
