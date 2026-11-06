"""Tests for server manager and transport selection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.server_manager import (
    _DEFAULT_LAZY_IDLE_SEC,
    ServerManager,
)
from mcp_router.transports.http import HttpTransport
from mcp_router.transports.sse import SseTransport
from mcp_router.transports.stdio import StdioTransport
from mcp_router.transports.websocket import WebSocketTransport

MOCK_STDIO = Path(__file__).parent / "fixtures" / "mock_stdio_server.py"


def test_detect_transport_types() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "stdio": McpServerConfig(name="stdio", command="echo"),
                "http": McpServerConfig(name="http", url="http://host/mcp"),
                "sse": McpServerConfig(name="sse", url="http://host/sse"),
                "ws": McpServerConfig(name="ws", url="ws://host/ws"),
                "explicit": McpServerConfig(name="explicit", type="http", command="ignored"),
            }
        )
    )
    servers = {s.name: s.type for s in manager.list_servers()}
    assert servers["stdio"] == "stdio"
    assert servers["http"] == "http"
    assert servers["sse"] == "sse"
    assert servers["ws"] == "websocket"
    assert servers["explicit"] == "http"


def test_create_transport_classes(sample_registry: Registry) -> None:
    manager = ServerManager()
    manager.load_registry(sample_registry)

    assert isinstance(
        manager._create_transport(sample_registry.mcp_servers["stdio-server"]), StdioTransport
    )
    assert isinstance(
        manager._create_transport(sample_registry.mcp_servers["http-server"]), HttpTransport
    )
    assert isinstance(
        manager._create_transport(sample_registry.mcp_servers["sse-server"]), SseTransport
    )
    assert isinstance(
        manager._create_transport(sample_registry.mcp_servers["ws-server"]), WebSocketTransport
    )


async def test_get_server_tools_uses_transport() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "mock": McpServerConfig(name="mock", type="http", url="http://mock/mcp"),
            }
        )
    )
    transport = MagicMock()
    transport.is_connected = False
    transport.connect = AsyncMock()
    transport.send_request = AsyncMock(
        return_value={"tools": [{"name": "tool_a", "description": "A"}]}
    )

    with patch.object(manager, "_create_transport", return_value=transport):
        tools = await manager.get_server_tools("mock")

    assert len(tools) == 1
    assert tools[0]["name"] == "tool_a"
    transport.connect.assert_awaited_once()
    transport.send_request.assert_awaited_once()


async def test_call_tool_passes_workspace_for_stdio() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "local": McpServerConfig(name="local", command="python3", args=["-c", "pass"]),
            }
        )
    )
    transport = MagicMock(spec=StdioTransport)
    transport.is_connected = True
    transport.send_request = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})

    with patch.object(manager, "_create_transport", return_value=transport):
        result = await manager.call_tool("local", "echo", {"text": "hi"}, workspace_path="/tmp/ws")

    transport.send_request.assert_awaited_once()
    assert transport.send_request.await_args is not None
    kwargs = transport.send_request.await_args.kwargs
    assert kwargs["extra_env"]["WORKSPACE_FOLDER"] == "/tmp/ws"
    assert result == [{"type": "text", "text": "ok"}]


def test_browser_servers_default_to_persistent_session() -> None:
    manager = ServerManager()
    for name in ("playwright", "chrome-devtools"):
        cfg = McpServerConfig(name=name, command="echo")
        transport = manager._create_transport(cfg)
        assert isinstance(transport, StdioTransport)
        assert transport.session_mode == "persistent"


def test_explicit_oneshot_overrides_browser_default() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(name="playwright", command="echo", session_mode="oneshot")
    transport = manager._create_transport(cfg)
    assert isinstance(transport, StdioTransport)
    assert transport.session_mode == "oneshot"


def test_light_stdio_defaults_to_oneshot() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(name="github", command="echo")
    transport = manager._create_transport(cfg)
    assert isinstance(transport, StdioTransport)
    assert transport.session_mode == "oneshot"


def test_lifecycle_lazy_maps_to_persistent_with_default_idle() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(name="github", command="echo", lifecycle="lazy")
    mode, idle = manager.resolve_stdio_lifecycle(cfg)
    assert mode == "persistent"
    assert idle == _DEFAULT_LAZY_IDLE_SEC
    transport = manager._create_transport(cfg)
    assert isinstance(transport, StdioTransport)
    assert transport.session_mode == "persistent"
    assert transport.idle_timeout_sec == _DEFAULT_LAZY_IDLE_SEC


def test_lifecycle_keep_alive_disables_idle_timeout() -> None:
    manager = ServerManager()
    for lifecycle in ("keep-alive", "lazy-keep-alive"):
        cfg = McpServerConfig(name="heavy", command="echo", lifecycle=lifecycle)
        mode, idle = manager.resolve_stdio_lifecycle(cfg)
        assert mode == "persistent"
        assert idle == 0.0
        transport = manager._create_transport(cfg)
        assert isinstance(transport, StdioTransport)
        assert transport.session_mode == "persistent"
        assert transport.idle_timeout_sec == 0.0


def test_lifecycle_idle_timeout_override() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(name="github", command="echo", lifecycle="lazy", idle_timeout_sec=120)
    _, idle = manager.resolve_stdio_lifecycle(cfg)
    assert idle == 120.0
    transport = manager._create_transport(cfg)
    assert isinstance(transport, StdioTransport)
    assert transport.idle_timeout_sec == 120.0


def test_lifecycle_takes_precedence_over_session_mode() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(
        name="github",
        command="echo",
        lifecycle="keep-alive",
        session_mode="oneshot",
    )
    mode, idle = manager.resolve_stdio_lifecycle(cfg)
    assert mode == "persistent"
    assert idle == 0.0


def test_unset_lifecycle_does_not_change_lan_oneshot_default() -> None:
    manager = ServerManager()
    cfg = McpServerConfig(name="searxng", command="echo")
    mode, idle = manager.resolve_stdio_lifecycle(cfg)
    assert mode == "oneshot"
    assert idle is None


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_lifecycle_lazy_idle_shutdown_via_manager() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "mock": McpServerConfig(
                    name="mock",
                    command="python3",
                    args=[str(MOCK_STDIO)],
                    lifecycle="lazy",
                    idle_timeout_sec=0.15,
                    timeout=10,
                ),
            }
        )
    )
    await manager.call_tool("mock", "pid", {})
    transport = manager._transports["mock"]
    assert isinstance(transport, StdioTransport)
    assert transport.has_live_process()
    await asyncio.sleep(0.35)
    assert not transport.has_live_process()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_lifecycle_keep_alive_probe_does_not_tear_session() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "mock": McpServerConfig(
                    name="mock",
                    command="python3",
                    args=[str(MOCK_STDIO)],
                    lifecycle="keep-alive",
                    timeout=10,
                ),
            }
        )
    )
    first = await manager.call_tool("mock", "pid", {})
    transport = manager._transports["mock"]
    assert isinstance(transport, StdioTransport)
    assert transport.has_live_process()
    pid_before = transport._process.pid if transport._process else None

    await manager.probe_server("mock")
    assert transport.has_live_process()
    assert transport._process is not None
    assert transport._process.pid == pid_before

    second = await manager.call_tool("mock", "pid", {})
    assert first == second
    await manager.cleanup_all()
