"""Tests for all MCP transport implementations."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from mcp_router.transports.base import BaseTransport
from mcp_router.transports.http import HttpTransport
from mcp_router.transports.sse import SseTransport
from mcp_router.transports.stdio import StdioTransport
from mcp_router.transports.websocket import WebSocketTransport


def test_base_transport_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseTransport()  # type: ignore[abstract]


@pytest.mark.parametrize(
    "cls",
    [StdioTransport, HttpTransport, SseTransport, WebSocketTransport],
)
def test_transport_constructors_share_config_signature(
    cls: type[StdioTransport | HttpTransport | SseTransport | WebSocketTransport],
) -> None:
    """All concrete transports take ``config: dict``; ABC ``__init__`` is argless."""
    params = list(inspect.signature(cls.__init__).parameters)
    assert params == ["self", "config"]
    assert list(inspect.signature(BaseTransport.__init__).parameters) == ["self"]


MOCK_STDIO = Path(__file__).parent / "fixtures" / "mock_stdio_server.py"
HTTP_URL = "http://mock.example/mcp"
SSE_URL = "http://mock.example/sse"
WS_URL = "ws://mock.example/ws"

TOOLS = [
    {
        "name": "search",
        "description": "Search tools",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def _jsonrpc_result(req_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _http_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return httpx.Response(
            200,
            json=_jsonrpc_result(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "mock-http"},
                },
            ),
            headers={"MCP-Session-Id": "sess-http"},
        )
    if method == "notifications/initialized":
        return httpx.Response(200, json={})
    if method == "tools/list":
        return httpx.Response(200, json=_jsonrpc_result(req_id, {"tools": TOOLS}))
    if method == "tools/call":
        return httpx.Response(
            200,
            json=_jsonrpc_result(req_id, {"content": [{"type": "text", "text": "http-ok"}]}),
        )
    return httpx.Response(400, json={"error": "unknown method"})


@respx.mock
async def test_http_transport_tools_list() -> None:
    respx.post(HTTP_URL).mock(side_effect=_http_handler)
    transport = HttpTransport({"url": HTTP_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert result["tools"][0]["name"] == "search"
    finally:
        await transport.close()


@respx.mock
async def test_http_transport_call_tool() -> None:
    respx.post(HTTP_URL).mock(side_effect=_http_handler)
    transport = HttpTransport({"url": HTTP_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {}},
            }
        )
        assert result["content"][0]["text"] == "http-ok"
    finally:
        await transport.close()


@respx.mock
async def test_http_transport_reinitializes_after_stale_session_404() -> None:
    """Downstream restart invalidates MCP-Session-Id → 404; recover once (WOR-1549)."""
    state = {"tools_calls": 0, "inits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        req_id = body.get("id")
        session = request.headers.get("mcp-session-id")

        if method == "initialize":
            state["inits"] += 1
            sid = f"sess-{state['inits']}"
            return httpx.Response(
                200,
                json=_jsonrpc_result(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "mock-http"},
                    },
                ),
                headers={"MCP-Session-Id": sid},
            )
        if method == "notifications/initialized":
            return httpx.Response(200, json={})
        if method == "tools/call":
            state["tools_calls"] += 1
            # First call after init uses sess-1; simulate server restart → unknown session.
            if session == "sess-1" and state["tools_calls"] == 1:
                return httpx.Response(404, text="Session not found")
            assert session == "sess-2"
            return httpx.Response(
                200,
                json=_jsonrpc_result(req_id, {"content": [{"type": "text", "text": "recovered"}]}),
            )
        return httpx.Response(400, json={"error": "unknown method"})

    respx.post(HTTP_URL).mock(side_effect=handler)
    transport = HttpTransport({"url": HTTP_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {}},
            }
        )
        assert result["content"][0]["text"] == "recovered"
        assert state["inits"] == 2
        assert state["tools_calls"] == 2
    finally:
        await transport.close()


@respx.mock
async def test_http_transport_reinitializes_after_incomplete_chunked_read() -> None:
    """Peer closed mid-body during downstream recreate → one reconnect retry."""
    state = {"tools_calls": 0, "inits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        req_id = body.get("id")

        if method == "initialize":
            state["inits"] += 1
            return httpx.Response(
                200,
                json=_jsonrpc_result(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "mock-http"},
                    },
                ),
                headers={"MCP-Session-Id": f"sess-{state['inits']}"},
            )
        if method == "notifications/initialized":
            return httpx.Response(200, json={})
        if method == "tools/call":
            state["tools_calls"] += 1
            if state["tools_calls"] == 1:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body "
                    "(incomplete chunked read)"
                )
            return httpx.Response(
                200,
                json=_jsonrpc_result(
                    req_id, {"content": [{"type": "text", "text": "after-reconnect"}]}
                ),
            )
        return httpx.Response(400, json={"error": "unknown method"})

    respx.post(HTTP_URL).mock(side_effect=handler)
    transport = HttpTransport({"url": HTTP_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {}},
            }
        )
        assert result["content"][0]["text"] == "after-reconnect"
        assert state["inits"] == 2
        assert state["tools_calls"] == 2
    finally:
        await transport.close()


async def test_sse_transport_connect_and_direct_response() -> None:
    transport = SseTransport({"url": SSE_URL})

    async def fake_read_sse(self: SseTransport, headers: dict[str, str]) -> None:
        self._session_id = "sess-sse"
        if self._sse_ready:
            self._sse_ready.set()

    with patch.object(SseTransport, "_read_sse", fake_read_sse):
        await transport.connect()
        assert transport.is_connected
        assert transport._session_id == "sess-sse"

        mock_response = httpx.Response(
            200,
            json=_jsonrpc_result(1, {"tools": TOOLS}),
        )
        transport._client = MagicMock()
        transport._client.post = AsyncMock(return_value=mock_response)
        transport._client.aclose = AsyncMock()
        result = await transport.send_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert result["tools"][0]["name"] == "search"
        await transport.close()


async def test_sse_transport_async_response_via_event() -> None:
    transport = SseTransport({"url": SSE_URL})
    transport._connected = True
    transport._session_id = "sess-sse"
    transport._mcp_url = "http://mock.example/messages"
    transport._client = MagicMock()
    transport._client.post = AsyncMock(return_value=httpx.Response(202, json={}))
    transport._client.aclose = AsyncMock()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    transport._pending[3] = future

    await transport._handle_event(
        "message",
        json.dumps(_jsonrpc_result(3, {"tools": TOOLS})),
    )
    result = await future
    assert result["tools"][0]["name"] == "search"


class _MockWebSocket:
    def __init__(self) -> None:
        self._next_response: str | None = None

    async def send(self, data: str) -> None:
        request = json.loads(data)
        method = request.get("method")
        req_id = request.get("id")
        if method == "tools/list":
            payload = _jsonrpc_result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            payload = _jsonrpc_result(req_id, {"content": [{"type": "text", "text": "ws-ok"}]})
        else:
            payload = _jsonrpc_result(req_id, {})
        self._next_response = json.dumps(payload)

    async def recv(self) -> str:
        assert self._next_response is not None
        return self._next_response

    async def close(self) -> None:
        return None


async def test_websocket_transport_tools_list(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_ws = _MockWebSocket()

    async def fake_connect(url: str, **kwargs: Any) -> _MockWebSocket:
        return mock_ws

    monkeypatch.setattr("mcp_router.transports.websocket.websockets.connect", fake_connect)
    transport = WebSocketTransport({"url": WS_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert result["tools"][0]["name"] == "search"
    finally:
        await transport.close()


async def test_websocket_transport_call_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_ws = _MockWebSocket()

    async def fake_connect(url: str, **kwargs: Any) -> _MockWebSocket:
        return mock_ws

    monkeypatch.setattr("mcp_router.transports.websocket.websockets.connect", fake_connect)
    transport = WebSocketTransport({"url": WS_URL})
    await transport.connect()
    try:
        result = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {}},
            }
        )
        assert result["content"][0]["text"] == "ws-ok"
    finally:
        await transport.close()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_stdio_transport_tools_list() -> None:
    transport = StdioTransport({"command": "python3", "args": [str(MOCK_STDIO)], "timeout": 10})
    await transport.connect()
    try:
        result = await transport.send_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert result["tools"][0]["name"] == "echo"
    finally:
        await transport.close()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_stdio_transport_call_tool() -> None:
    transport = StdioTransport({"command": "python3", "args": [str(MOCK_STDIO)], "timeout": 10})
    await transport.connect()
    try:
        result = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            }
        )
        assert result["content"][0]["text"] == "stdio-ok"
    finally:
        await transport.close()


def _open_fd_count() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
@pytest.mark.skipif(
    not Path(f"/proc/{os.getpid()}/fd").exists(), reason="/proc fd listing unavailable"
)
async def test_stdio_transport_no_fd_leak_across_requests() -> None:
    """After N stdio round-trips, open FD count must stay stable (A1)."""
    transport = StdioTransport({"command": "python3", "args": [str(MOCK_STDIO)], "timeout": 10})
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    await transport.connect()
    try:
        # Warm up so interpreter / asyncio bookkeeping settles.
        assert (await transport.send_request(request))["tools"][0]["name"] == "echo"
        gc.collect()
        await asyncio.sleep(0)
        baseline = _open_fd_count()

        for i in range(25):
            req = {**request, "id": i + 2}
            result = await transport.send_request(req)
            assert result["tools"][0]["name"] == "echo"

        gc.collect()
        await asyncio.sleep(0)
        after = _open_fd_count()
        # Allow tiny noise; a real pipe leak grows by ~3 FDs per request.
        assert after <= baseline + 3, f"FD leak: baseline={baseline} after={after}"
    finally:
        await transport.close()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_persistent_stdio_peak_one_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """WOR-874: N sequential calls to persistent stdio ⇒ spawn count / peak = 1."""
    spawn_count = 0
    peak_live = 0
    live = 0
    real_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawn_count, peak_live, live
        spawn_count += 1
        live += 1
        peak_live = max(peak_live, live)
        proc = await real_exec(*args, **kwargs)

        class _Wrapped:
            def __getattr__(self, name: str) -> Any:
                return getattr(proc, name)

            async def wait(self) -> int:
                nonlocal live
                code = await proc.wait()
                live = max(0, live - 1)
                return code

        return _Wrapped()  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    transport = StdioTransport(
        {
            "command": "python3",
            "args": [str(MOCK_STDIO)],
            "timeout": 10,
            "session_mode": "persistent",
            "idle_timeout_sec": 60,
        }
    )
    await transport.connect()
    try:
        pids: list[str] = []
        for i in range(5):
            result = await transport.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "tools/call",
                    "params": {"name": "pid", "arguments": {}},
                }
            )
            pids.append(result["content"][0]["text"])
        assert spawn_count == 1
        assert peak_live == 1
        assert len(set(pids)) == 1
        assert transport.has_live_process()
    finally:
        await transport.close()
    assert not transport.has_live_process()
    assert spawn_count == 1


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_persistent_stdio_keeps_in_process_state() -> None:
    transport = StdioTransport(
        {
            "command": "python3",
            "args": [str(MOCK_STDIO)],
            "timeout": 10,
            "session_mode": "persistent",
        }
    )
    await transport.connect()
    try:
        await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "set_token", "arguments": {"token": "keep-me"}},
            }
        )
        got = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_token", "arguments": {}},
            }
        )
        assert got["content"][0]["text"] == "keep-me"
    finally:
        await transport.close()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_oneshot_stdio_does_not_share_state() -> None:
    transport = StdioTransport(
        {
            "command": "python3",
            "args": [str(MOCK_STDIO)],
            "timeout": 10,
            "session_mode": "oneshot",
        }
    )
    await transport.connect()
    try:
        await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "set_token", "arguments": {"token": "gone"}},
            }
        )
        got = await transport.send_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_token", "arguments": {}},
            }
        )
        assert got["content"][0]["text"] == "None"
    finally:
        await transport.close()


@pytest.mark.skipif(not MOCK_STDIO.exists(), reason="mock stdio server missing")
async def test_persistent_stdio_idle_timeout_closes_process() -> None:
    transport = StdioTransport(
        {
            "command": "python3",
            "args": [str(MOCK_STDIO)],
            "timeout": 10,
            "session_mode": "persistent",
            "idle_timeout_sec": 0.15,
        }
    )
    await transport.connect()
    try:
        await transport.send_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert transport.has_live_process()
        await asyncio.sleep(0.35)
        assert not transport.has_live_process()
    finally:
        await transport.close()
