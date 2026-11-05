"""SSE transport for MCP protocol."""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any, cast

import httpx

from mcp_router.transports.base import BaseTransport

logger = logging.getLogger(__name__)


class SseTransport(BaseTransport):
    """SSE transport for session-based MCP servers."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.url = config.get("url")
        self.headers = config.get("headers", {})
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._mcp_url: str | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_ready: asyncio.Event | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        if not self.url or not self.url.endswith("/sse"):
            raise ValueError("SSE transport requires url ending with /sse")

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.url:
            raise RuntimeError("SSE URL not configured")
        base = self.url.removesuffix("/sse").rstrip("/")
        self._mcp_url = f"{base}/messages"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True
        )
        self._sse_ready = asyncio.Event()
        headers = {"Accept": "text/event-stream", **self.headers}
        self._sse_task = asyncio.create_task(self._read_sse(headers))
        try:
            await asyncio.wait_for(self._sse_ready.wait(), timeout=15.0)
        except TimeoutError as exc:
            await self._cancel_sse()
            raise RuntimeError("SSE connection timeout") from exc
        if not self._session_id:
            self._session_id = str(uuid.uuid4())
        self._connected = True

    async def _read_sse(self, headers: dict[str, str]) -> None:
        assert self._client and self.url
        async with self._client.stream("GET", self.url, headers=headers) as response:
            response.raise_for_status()
            event = ""
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    await self._handle_event(event, payload)
                    event = ""

    async def _handle_event(self, event: str, payload: str) -> None:
        if event == "endpoint":
            if "session_id=" in payload:
                self._session_id = payload.split("session_id=")[-1].split("&")[0]
            if self._mcp_url and "?" not in self._mcp_url:
                self._mcp_url = payload if payload.startswith("http") else self._mcp_url
            if self._sse_ready:
                self._sse_ready.set()
            return
        if event in ("message", ""):
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                return
            req_id = msg.get("id")
            if req_id in self._pending and not self._pending[req_id].done():
                if "result" in msg:
                    self._pending[req_id].set_result(msg["result"])
                elif "error" in msg:
                    self._pending[req_id].set_exception(RuntimeError(str(msg["error"])))

    async def send_request(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not self._connected:
            await self.connect()
        assert self._client and self._mcp_url
        req_id = request.get("id")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        if req_id is not None:
            self._pending[req_id] = future
        url = self._mcp_url
        if self._session_id and "session_id=" not in (url or ""):
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}session_id={self._session_id}"
        headers = {"Content-Type": "application/json", **self.headers}
        response = await self._client.post(url, json=request, headers=headers)
        if response.status_code == 202 and req_id is not None:
            try:
                return await asyncio.wait_for(future, timeout=120.0)
            finally:
                self._pending.pop(req_id, None)
        data = response.json()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        result = data.get("result", data)
        return cast(dict[str, Any], result)

    async def _cancel_sse(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sse_task

    async def close(self) -> None:
        await self._cancel_sse()
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
