"""ASGI / Starlette middleware for the HTTP adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from mcp_router.services.search_telemetry import (
    caller_id_from_headers,
    set_caller_id,
)
from mcp_router.services.strict_policy import set_surface


class DualMcpSurfaceMiddleware:
    """Map /mcp/v2 → /mcp while marking the request as the strict surface."""

    def __init__(self, app: ASGIApp, *, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and self.enabled:
            path = scope.get("path") or ""
            if path == "/mcp/v2" or path.startswith("/mcp/v2/"):
                set_surface("strict")
                new_path = "/mcp" + path[len("/mcp/v2") :]
                scope = dict(scope)
                scope["path"] = new_path or "/mcp"
                raw = scope.get("raw_path")
                if isinstance(raw, (bytes, bytearray)):
                    suffix = path[len("/mcp/v2") :].encode("utf-8")
                    scope["raw_path"] = b"/mcp" + suffix
            else:
                set_surface("default")
        elif scope["type"] in ("http", "websocket"):
            set_surface("default")
        await self.app(scope, receive, send)


class CallerIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        set_caller_id(
            caller_id_from_headers(
                request.headers.get("Authorization"),
                request.headers.get("X-MCP-Caller-Id"),
            )
        )
        return await call_next(request)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, api_key: str) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self.api_key:
            return await call_next(request)
        if request.url.path in ("/health", "/status"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.api_key}":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)
