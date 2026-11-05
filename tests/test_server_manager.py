"""Tests for server manager and transport selection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.server_manager import ServerManager
from mcp_router.transports.http import HttpTransport
from mcp_router.transports.sse import SseTransport
from mcp_router.transports.stdio import StdioTransport
from mcp_router.transports.websocket import WebSocketTransport


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
