"""WebSocket transport for MCP protocol."""

import json
import logging
from typing import Any, cast

import websockets

from mcp_router.transports.base import BaseTransport

logger = logging.getLogger(__name__)


class WebSocketTransport(BaseTransport):
    """WebSocket transport for MCP servers."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.url = config.get("url")
        self.headers = config.get("headers", {})
        self._ws: Any = None
        if not self.url:
            raise ValueError("WebSocket transport requires 'url'")

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.url:
            raise RuntimeError("WebSocket URL not configured")
        extra_headers = {k: v for k, v in self.headers.items()}
        self._ws = await websockets.connect(self.url, additional_headers=extra_headers)
        self._connected = True

    async def send_request(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not self._connected or not self._ws:
            await self.connect()
        assert self._ws
        await self._ws.send(json.dumps(request))
        raw = await self._ws.recv()
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        result = data.get("result", data)
        return cast(dict[str, Any], result)

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
