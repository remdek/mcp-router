"""Day-path quarantine for unhealthy downstream MCP servers (WOR-1092).

Failed probes (see ``DownstreamHealthProber``) must not be offered as call
targets on the agent day-path. Search marks them ``day_path=skip``; tool
search drops their tools; ``call_server_tool`` returns ``server_unhealthy``.

Rollback: set ``HEALTH_QUARANTINE_ENABLED=false`` (keeps probes/status).
"""

from __future__ import annotations

from typing import Any

from mcp_router.services.health_probe import DownstreamHealthProber


def failed_server_names(prober: DownstreamHealthProber | None) -> set[str]:
    """Return servers known-unhealthy from the last probe snapshot.

    Empty before the first probe completes — do not quarantine unknowns.
    """
    if prober is None:
        return set()
    snap = prober.snapshot()
    return set(snap.get("failed_servers") or [])


def probe_errors(prober: DownstreamHealthProber | None) -> dict[str, str]:
    if prober is None:
        return {}
    servers = prober.snapshot().get("servers") or {}
    out: dict[str, str] = {}
    for name, info in servers.items():
        if not isinstance(info, dict):
            continue
        if info.get("healthy") is False and info.get("error"):
            out[str(name)] = str(info["error"])
    return out


def filter_tool_results(
    results: list[dict[str, Any]],
    quarantined: set[str],
) -> list[dict[str, Any]]:
    if not quarantined:
        return results
    return [r for r in results if str(r.get("server_name", "")) not in quarantined]


def annotate_server_results(
    results: list[dict[str, Any]],
    quarantined: set[str],
    *,
    errors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Attach healthy / day_path markers; keep unhealthy visible for ops."""
    err = errors or {}
    annotated: list[dict[str, Any]] = []
    for item in results:
        row = dict(item)
        name = str(row.get("server_name", ""))
        if name in quarantined:
            row["healthy"] = False
            row["day_path"] = "skip"
            if name in err:
                row["health_error"] = err[name]
        else:
            row["healthy"] = True
            row["day_path"] = "ok"
        annotated.append(row)
    return annotated


def is_server_quarantined(
    server_name: str,
    quarantined: set[str],
    *,
    enabled: bool,
) -> bool:
    return bool(enabled and server_name in quarantined)
