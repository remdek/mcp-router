"""Normalized MCP tool responses and typed remediation errors."""

from __future__ import annotations

import re
from typing import Any, Literal

ErrorSource = Literal["router", "downstream", "tool"]

FailureKind = Literal[
    "downstream_unreachable",
    "auth_failed",
    "index_degraded",
    "tool_not_found",
    "timeout",
    "strict_denied",
    "server_unhealthy",
]

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer\s+)[a-z0-9._\-+=/]+"),
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(x-api-key\s*[:=]\s*)\S+"),
    # Bare token shapes (no key= prefix) — OpenAI/Anthropic, GitHub, AWS, JWT, Slack, GitLab
    re.compile(r"\b(sk-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(AKIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(glpat-)[A-Za-z0-9_-]{20,}"),
)


def _redact_secret(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}[REDACTED]"
    return "[REDACTED]"


_FAILURE_HINTS: dict[FailureKind, tuple[str, str]] = {
    "downstream_unreachable": (
        "Downstream MCP is unreachable from the router host. "
        "Verify the process/container is up, registry URL/command, and network.",
        "Check registry entry and host reachability; call get_router_status; retry once.",
    ),
    "auth_failed": (
        "Downstream rejected credentials. Fix API keys/env on the router host "
        "(registry headers / .env) — never in the client MCP config.",
        "Update server-side secrets in router .env/registry, then reconnect MCP.",
    ),
    "index_degraded": (
        "Tool index is missing, rebuilding, or unhealthy — search results may be empty/stale.",
        "Wait for index_status ready, or POST /admin/reindex, then retry search_tools.",
    ),
    "tool_not_found": (
        "Server or tool name is unknown to the router registry/index.",
        "Use search_servers / search_tools (and aliases) to confirm exact names, then retry.",
    ),
    "timeout": (
        "Downstream call exceeded the configured timeout.",
        "Retry once; if it persists, raise registry timeout or check downstream health.",
    ),
    "strict_denied": (
        "Call blocked by the strict MCP surface (/mcp/v2) allow-list or read-only profile.",
        "Use full /mcp, or add the server/tool to STRICT_ALLOWLIST / registry strictProfile.",
    ),
    "server_unhealthy": (
        "Downstream server failed the last health probe and is quarantined from the day-path. "
        "See get_router_status.downstream_health and the unhealthy-server runbook.",
        "Pick a healthy server via search_tools, or fix/re-enable the downstream; "
        "rollback quarantine with HEALTH_QUARANTINE_ENABLED=false.",
    ),
}


def sanitize_error_message(message: str) -> str:
    """Strip credentials/headers from messages before returning them to agents."""
    cleaned = message
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(_redact_secret, cleaned)
    return cleaned


def classify_failure_kind(exc: Exception) -> FailureKind:
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, ValueError) and "not found" in lowered:
        return "tool_not_found"
    if "unauthorized" in lowered or "401" in lowered or "403" in lowered:
        return "auth_failed"
    if "timeout" in lowered or isinstance(exc, TimeoutError):
        return "timeout"
    if any(
        token in lowered
        for token in (
            "connection",
            "connect",
            "refused",
            "econnrefused",
            "unreachable",
            "name or service not known",
            "nodename nor servname",
            "network is unreachable",
        )
    ):
        return "downstream_unreachable"
    if "not found" in lowered:
        return "tool_not_found"
    return "downstream_unreachable"


def failure_to_error_source(kind: FailureKind) -> ErrorSource:
    if kind in ("tool_not_found", "index_degraded", "strict_denied", "server_unhealthy"):
        return "router"
    return "downstream"


def classify_call_error(exc: Exception, server_name: str) -> tuple[ErrorSource, str]:
    """Backward-compatible classifier: error_source + sanitized message."""
    del server_name  # kept for call-site compatibility
    kind = classify_failure_kind(exc)
    return failure_to_error_source(kind), sanitize_error_message(str(exc))


def remediation_error(
    kind: FailureKind,
    *,
    error: str,
    server_name: str | None = None,
    tool_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Typed remediation payload (treg-style hints, no secrets)."""
    reconnect_hint, next_action = _FAILURE_HINTS[kind]
    payload: dict[str, Any] = {
        "ok": False,
        "failure_kind": kind,
        "error": sanitize_error_message(error),
        "error_source": failure_to_error_source(kind),
        "server_name": server_name,
        "reconnect_hint": reconnect_hint,
        "next_action": next_action,
        "retry_hint": next_action,
    }
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if extra:
        payload.update(extra)
    return payload


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Normalize downstream tool output to a stable proxy shape."""
    if isinstance(result, dict):
        content = result.get("content")
        if content is None and "result" in result:
            content = result.get("result")
        if content is None:
            content = result
        text_blocks = _extract_text_blocks(content)
        return {
            "content": content,
            "text": "\n".join(text_blocks) if text_blocks else None,
            "is_error": bool(result.get("isError")),
        }
    if isinstance(result, list):
        text_blocks = _extract_text_blocks(result)
        return {
            "content": result,
            "text": "\n".join(text_blocks) if text_blocks else None,
            "is_error": False,
        }
    return {
        "content": result,
        "text": str(result) if result is not None else None,
        "is_error": False,
    }


def _extract_text_blocks(content: Any) -> list[str]:
    if isinstance(content, list):
        blocks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    blocks.append(text)
        return blocks
    if isinstance(content, str):
        return [content]
    return []


Confidence = Literal["none", "low", "medium", "high"]


def assess_search_confidence(results: list[dict[str, Any]]) -> tuple[Confidence, str]:
    if not results:
        return "none", "No matches. Try search_servers or a broader query."
    top = float(results[0].get("score", 0))
    second = float(results[1].get("score", 0)) if len(results) > 1 else 0.0
    if top >= 0.04 and (second == 0 or top >= second * 1.4):
        return "high", "Strong match — top result is clearly ahead."
    if top >= 0.02:
        return "medium", "Possible match — verify server:tool before calling."
    return "low", "Weak match — consider search_servers or rephrase the query."


def group_results_by_server(
    results: list[dict[str, Any]], max_per_server: int = 2
) -> list[dict[str, Any]]:
    if max_per_server < 1:
        return results
    counts: dict[str, int] = {}
    grouped: list[dict[str, Any]] = []
    for item in results:
        server = str(item.get("server_name", ""))
        if counts.get(server, 0) >= max_per_server:
            continue
        counts[server] = counts.get(server, 0) + 1
        grouped.append(item)
    return grouped
