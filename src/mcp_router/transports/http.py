"""HTTP transport for MCP protocol (Streamable HTTP with session support)."""

import json
import logging
from typing import Any, cast

import httpx

from mcp_router.transports.base import BaseTransport

logger = logging.getLogger(__name__)


class HttpTransport(BaseTransport):
    """HTTP transport for remote MCP servers (Streamable HTTP)."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.url = config.get("url")
        self.headers = config.get("headers", {})
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._initialized = False
        if not self.url:
            raise ValueError("HTTP transport requires 'url' in config")
        if self.url.endswith("/sse"):
            raise ValueError("Use SseTransport for /sse URLs")

    async def connect(self) -> None:
        if self._connected:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True
        )
        self._connected = True

    async def send_request(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not self._connected or not self._client:
            await self.connect()

        method = request.get("method", "")
        if method != "initialize" and not self._initialized:
            await self._initialize_session()

        headers = self._build_headers()
        assert self._client and self.url
        response = await self._client.post(self.url, json=request, headers=headers)

        if response.status_code == 401:
            raise RuntimeError(f"Unauthorized for {self.url}: check API key headers")

        response.raise_for_status()
        data = self._parse_response_body(response)
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"MCP error [{err.get('code', -1)}]: {err.get('message', 'Unknown')}"
            )
        if "result" not in data:
            raise RuntimeError(f"Invalid JSON-RPC response: {data}")
        return cast(dict[str, Any], data["result"])

    async def _initialize_session(self) -> None:
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-router", "version": "0.1.0"},
            },
        }
        headers = self._build_headers()
        assert self._client and self.url
        response = await self._client.post(self.url, json=init_request, headers=headers)
        response.raise_for_status()

        session_id = response.headers.get("mcp-session-id") or response.headers.get(
            "MCP-Session-Id"
        )
        if session_id:
            self._session_id = session_id

        initialized = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        headers = self._build_headers()
        await self._client.post(self.url, json=initialized, headers=headers)
        self._initialized = True

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    def _parse_response_body(self, response: httpx.Response) -> dict[str, Any]:
        session_id = response.headers.get("mcp-session-id") or response.headers.get(
            "MCP-Session-Id"
        )
        if session_id:
            self._session_id = session_id

        text = response.text.strip()
        if not text:
            return {}

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or text.startswith("event:"):
            return self._parse_sse_payload(text)

        try:
            return cast(dict[str, Any], response.json())
        except json.JSONDecodeError:
            return self._parse_sse_payload(text)

    def _parse_sse_payload(self, text: str) -> dict[str, Any]:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return cast(dict[str, Any], json.loads(payload))
        raise RuntimeError(f"Could not parse SSE response: {text[:500]}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None
        self._initialized = False
        self._connected = False
