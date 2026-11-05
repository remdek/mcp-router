"""Tests for day-path quarantine of unhealthy downstream servers (WOR-1092)."""

from __future__ import annotations

from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.health_probe import DownstreamHealthProber, ServerProbeResult
from mcp_router.services.health_quarantine import (
    annotate_server_results,
    failed_server_names,
    filter_tool_results,
    is_server_quarantined,
)
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore


def _prober_with_results(results: dict[str, ServerProbeResult]) -> DownstreamHealthProber:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                name: McpServerConfig(name=name, type="http", url=f"http://mock/{name}")
                for name in results
            }
        )
    )
    prober = DownstreamHealthProber(manager, enabled=False)
    prober._state.results = results
    return prober


def test_failed_server_names_only_known_unhealthy() -> None:
    prober = _prober_with_results(
        {
            "ok": ServerProbeResult(server_name="ok", healthy=True),
            "broken": ServerProbeResult(server_name="broken", healthy=False, error="down"),
        }
    )
    assert failed_server_names(prober) == {"broken"}


def test_filter_tool_results_drops_unhealthy() -> None:
    results = [
        {"server_name": "ok", "tool_name": "a", "score": 0.1},
        {"server_name": "broken", "tool_name": "b", "score": 0.09},
        {"server_name": "ok", "tool_name": "c", "score": 0.08},
    ]
    filtered = filter_tool_results(results, {"broken"})
    assert [r["tool_name"] for r in filtered] == ["a", "c"]


def test_annotate_server_results_marks_unhealthy() -> None:
    results = [
        {"server_name": "ok", "description": "fine"},
        {"server_name": "broken", "description": "dead"},
    ]
    annotated = annotate_server_results(results, {"broken"})
    assert annotated[0]["healthy"] is True
    assert annotated[0]["day_path"] == "ok"
    assert annotated[1]["healthy"] is False
    assert annotated[1]["day_path"] == "skip"
    assert "health_error" not in annotated[1]


def test_annotate_uses_probe_error_when_provided() -> None:
    results = [{"server_name": "broken", "description": "dead"}]
    errors = {"broken": "name resolution failed"}
    annotated = annotate_server_results(results, {"broken"}, errors=errors)
    assert annotated[0]["healthy"] is False
    assert annotated[0]["day_path"] == "skip"
    assert annotated[0]["health_error"] == "name resolution failed"


def test_is_server_quarantined_respects_enabled_flag() -> None:
    assert is_server_quarantined("broken", {"broken"}, enabled=True) is True
    assert is_server_quarantined("broken", {"broken"}, enabled=False) is False
    assert is_server_quarantined("ok", {"broken"}, enabled=True) is False
    assert is_server_quarantined("unknown", set(), enabled=True) is False


def test_search_servers_annotation_integration(tmp_db: SqliteStore) -> None:
    from mcp_router.services.server_search import search_servers

    tmp_db.upsert_tools([])
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "docs": McpServerConfig(
                    name="docs",
                    type="http",
                    url="http://mock",
                    description="Documentation server",
                ),
                "penpot": McpServerConfig(
                    name="penpot",
                    type="http",
                    url="http://mock/penpot",
                    description="Design platform",
                ),
            }
        )
    )
    raw = search_servers(manager, tmp_db, query="")
    annotated = annotate_server_results(raw, {"penpot"}, errors={"penpot": "dns"})
    by_name = {r["server_name"]: r for r in annotated}
    assert by_name["docs"]["day_path"] == "ok"
    assert by_name["penpot"]["day_path"] == "skip"
    assert by_name["penpot"]["healthy"] is False
