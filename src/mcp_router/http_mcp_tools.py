"""MCP tool handlers registered on the router FastMCP instance."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_router.services import failure_backoff as failure_backoff_svc
from mcp_router.services.feedback import (
    FeedbackValidationError,
    ReviewTargetNotFound,
)
from mcp_router.services.feedback_contract import (
    FEEDBACK_DESCRIPTION,
    REVIEW_DESCRIPTION,
    FeedbackCategory,
    ReviewUsefulness,
)
from mcp_router.services.health_quarantine import (
    annotate_server_results,
    failed_server_names,
    filter_tool_results,
    is_server_quarantined,
    probe_errors,
)
from mcp_router.services.output_guard import apply_output_guard, default_artifacts_dir
from mcp_router.services.responses import (
    assess_search_confidence,
    classify_failure_kind,
    group_results_by_server,
    normalize_tool_result,
    remediation_error,
)
from mcp_router.services.schema_budget import apply_schema_on_demand
from mcp_router.services.server_search import search_servers as search_registered_servers
from mcp_router.services.strict_policy import get_surface


def register_mcp_tools(mcp: FastMCP) -> None:
    """Attach router MCP tools; state is resolved lazily from http_server."""

    @mcp.tool()
    async def get_router_status() -> str:
        """Router health: index state, embedding mode, reindex progress, downstream probes, reconnect hints."""
        from mcp_router import http_server as hs

        return json.dumps(await hs._router_status_snapshot(), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def search_servers(query: str = "", limit: int = 20) -> str:
        """Search registered downstream MCP servers (not individual tools)."""
        from mcp_router import http_server as hs

        state = hs.state
        started = time.perf_counter()
        results = search_registered_servers(
            state.server_manager, state.store, query=query, limit=limit
        )
        if get_surface() == "strict":
            results = state.strict_policy.filter_server_results(results)[:limit]
        if state.settings.health_quarantine_enabled:
            results = annotate_server_results(
                results,
                failed_server_names(state.health_prober),
                errors=probe_errors(state.health_prober),
            )
        results = failure_backoff_svc.annotate_server_results(results, state.failure_backoff)
        latency_ms = (time.perf_counter() - started) * 1000.0
        search_id = state.search_telemetry.log_search(
            kind="servers",
            query=query,
            results=results,
            latency_ms=latency_ms,
        )
        payload = {
            "query": query,
            "search_id": search_id,
            "results": results,
            "router": await hs._router_status_snapshot(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def search_tools(
        query: str,
        limit: int = 10,
        vector_weight: float = 0.4,
        fulltext_weight: float = 0.6,
        max_per_server: int | None = None,
        include_schemas: bool = False,
        include_schema_for: list[str] | None = None,
    ) -> str:
        """Search MCP tools by semantic or keyword query (RU/EN).

        By default only strong matches include full ``tool_schema``; weak hits
        are identity + description (``schema_omitted=true``). Pass
        ``include_schemas=true`` for every schema, or ``include_schema_for`` with
        ``server:tool`` / ``tool_id`` / ``tool_name`` tokens. Use
        ``get_tool_schema`` for a single weak hit without re-searching.
        """
        from mcp_router import http_server as hs

        state = hs.state
        if state.tool_search is None:
            return json.dumps(
                remediation_error(
                    "index_degraded",
                    error="search not initialized",
                ),
                ensure_ascii=False,
            )
        raw_limit = limit * 3
        started = time.perf_counter()
        results, shadow = await state.tool_search.search_with_shadow(
            query=query,
            limit=raw_limit,
            vector_weight=vector_weight,
            fulltext_weight=fulltext_weight,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        per_server = max_per_server or state.settings.search_max_per_server
        if state.settings.health_quarantine_enabled:
            quarantine = failed_server_names(state.health_prober)
            results = filter_tool_results(results, quarantine)
            if shadow is not None:
                shadow = filter_tool_results(shadow, quarantine)
        backoff = state.failure_backoff.active_servers()
        if backoff:
            results = failure_backoff_svc.filter_tool_results(results, backoff)
            if shadow is not None:
                shadow = failure_backoff_svc.filter_tool_results(shadow, backoff)
        grouped = group_results_by_server(results, max_per_server=per_server)[:limit]
        if get_surface() == "strict":
            grouped = state.strict_policy.filter_tool_results(grouped)[:limit]
        shadow_grouped = None
        if shadow is not None:
            shadow_grouped = group_results_by_server(shadow, max_per_server=per_server)[:limit]
            if get_surface() == "strict":
                shadow_grouped = state.strict_policy.filter_tool_results(shadow_grouped)[:limit]
        search_id = state.search_telemetry.log_search(
            kind="tools",
            query=query,
            results=grouped,
            latency_ms=latency_ms,
            shadow_results=shadow_grouped,
        )
        # Gate schemas after strict filter (read-only needs full schemas to decide).
        grouped = apply_schema_on_demand(
            grouped,
            ratio=state.settings.search_strong_match_ratio,
            include_schemas=include_schemas,
            include_schema_for=include_schema_for,
            enabled=state.settings.search_schema_on_demand,
        )
        confidence, confidence_hint = assess_search_confidence(grouped)
        router = await hs._router_status_snapshot()
        index_status_name = router["index_status"]["status"]
        index_degraded = (
            router["reindex_in_progress"]
            or index_status_name in ("error", "indexing", "pending")
            or (confidence == "none" and int(router["tool_count"] or 0) == 0)
        )
        if router["reindex_in_progress"]:
            confidence_hint = f"{confidence_hint} Index rebuild in progress — results may be stale."

        fb_snap = state.failure_backoff.snapshot()
        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "search_id": search_id,
            "confidence": confidence,
            "confidence_hint": confidence_hint,
            "results": grouped,
            "router": {
                "reindex_in_progress": router["reindex_in_progress"],
                "index_status": index_status_name,
                "tool_count": router["tool_count"],
            },
        }
        if fb_snap.get("active"):
            # Short agent-visible hint (names + message); full detail in get_router_status.
            payload["router"]["failure_backoff"] = {
                "active": fb_snap["active"],
                "servers": {
                    name: info.get("message")
                    for name, info in (fb_snap.get("servers") or {}).items()
                },
            }
        if index_degraded:
            rem = remediation_error(
                "index_degraded",
                error=f"index_status={index_status_name}; tool_count={router['tool_count']}",
            )
            payload["failure_kind"] = rem["failure_kind"]
            payload["reconnect_hint"] = rem["reconnect_hint"]
            payload["next_action"] = rem["next_action"]

        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def get_tool_schema(server_name: str, tool_name: str) -> str:
        """Fetch full tool_schema for one indexed tool (schema-on-demand lookup)."""
        from mcp_router import http_server as hs

        schema = hs._lookup_tool_schema(server_name, tool_name)
        if schema is None:
            return json.dumps(
                remediation_error(
                    "tool_not_found",
                    error=f"No indexed schema for {server_name}:{tool_name}",
                    server_name=server_name,
                    tool_name=tool_name,
                ),
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "server_name": server_name,
                "tool_name": tool_name,
                "tool_id": f"{server_name}:{tool_name}",
                "tool_schema": schema,
            },
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    async def call_server_tool(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Call a tool on a downstream MCP server."""
        from mcp_router import http_server as hs

        state = hs.state
        args = arguments or {}
        call_id = state.search_telemetry.log_call_audit(server_name, tool_name, ok=None)
        if get_surface() == "strict":
            schema = hs._lookup_tool_schema(server_name, tool_name)
            allowed, reason = state.strict_policy.allows_tool(
                server_name, tool_name, tool_schema=schema
            )
            if not allowed:
                return json.dumps(
                    {
                        **remediation_error(
                            "strict_denied",
                            error=reason,
                            server_name=server_name,
                            tool_name=tool_name,
                        ),
                        "call_id": call_id,
                    },
                    ensure_ascii=False,
                )
        if state.server_manager.get_server_config(server_name) is None:
            return json.dumps(
                {
                    **remediation_error(
                        "tool_not_found",
                        error=f"Server not found in registry: {server_name}",
                        server_name=server_name,
                        tool_name=tool_name,
                    ),
                    "call_id": call_id,
                },
                ensure_ascii=False,
            )
        # Fast layer: recent call/connect failure (agent-visible reason).
        backoff_msg = state.failure_backoff.describe(server_name)
        if backoff_msg is not None:
            return json.dumps(
                {
                    **remediation_error(
                        "failure_backoff",
                        error=f"Server in failure backoff: {server_name}: {backoff_msg}",
                        server_name=server_name,
                        tool_name=tool_name,
                    ),
                    "call_id": call_id,
                },
                ensure_ascii=False,
            )
        quarantine = failed_server_names(state.health_prober)
        if is_server_quarantined(
            server_name,
            quarantine,
            enabled=state.settings.health_quarantine_enabled,
        ):
            err = probe_errors(state.health_prober).get(server_name) or "failed last health probe"
            return json.dumps(
                {
                    **remediation_error(
                        "server_unhealthy",
                        error=f"Server quarantined (unhealthy): {server_name}: {err}",
                        server_name=server_name,
                        tool_name=tool_name,
                    ),
                    "call_id": call_id,
                },
                ensure_ascii=False,
            )
        try:
            raw = await state.server_manager.call_tool(server_name, tool_name, args)
            normalized = normalize_tool_result(raw)
            # Successful connect/call clears the short backoff window.
            if not normalized["is_error"]:
                state.failure_backoff.clear(server_name)
            payload: dict[str, Any] = {
                "ok": not normalized["is_error"],
                "call_id": call_id,
                "server_name": server_name,
                "tool_name": tool_name,
                "result": normalized,
                "error_source": "tool" if normalized["is_error"] else None,
            }
            artifacts = (
                Path(state.settings.output_guard_dir).expanduser()
                if state.settings.output_guard_dir.strip()
                else default_artifacts_dir(state.settings.index_db_path)
            )
            payload = apply_output_guard(
                payload,
                enabled=state.settings.output_guard_enabled,
                max_bytes=state.settings.output_guard_max_bytes,
                max_lines=state.settings.output_guard_max_lines,
                artifacts_dir=artifacts,
                call_id=call_id or "unknown",
            )
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as exc:
            kind = classify_failure_kind(exc)
            state.failure_backoff.record(server_name, str(exc), failure_kind=kind)
            return json.dumps(
                {
                    **remediation_error(
                        kind,
                        error=str(exc),
                        server_name=server_name,
                        tool_name=tool_name,
                    ),
                    "call_id": call_id,
                },
                ensure_ascii=False,
            )

    @mcp.tool(description=FEEDBACK_DESCRIPTION)
    async def feedback(
        category: FeedbackCategory,
        message: str,
        call_ids: list[str] | None = None,
        search_ids: list[str] | None = None,
    ) -> str:
        """Local feedback intake (quality / friction / other). Does not call downstream."""
        from mcp_router import http_server as hs

        try:
            result = hs.state.feedback_service.submit_feedback(
                category=category,
                message=message,
                call_ids=call_ids,
                search_ids=search_ids,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except FeedbackValidationError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @mcp.tool(description=REVIEW_DESCRIPTION)
    async def review(
        usefulness: ReviewUsefulness,
        call_id: str | None = None,
        search_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        """Rate a prior call or search. Local SQLite write only — no downstream."""
        from mcp_router import http_server as hs

        try:
            result = hs.state.feedback_service.submit_review(
                usefulness=usefulness,
                call_id=call_id,
                search_id=search_id,
                reason=reason,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except ReviewTargetNotFound as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except FeedbackValidationError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
