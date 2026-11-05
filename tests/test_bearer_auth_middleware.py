"""Bearer auth must cover /mcp and /mcp/v2 after Dual path rewrite (WOR-834 / A4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from mcp_router.http_server import BearerAuthMiddleware, DualMcpSurfaceMiddleware

API_KEY = "test-secret-key"


class _RecordingBearer(BearerAuthMiddleware):
    """Records the path Bearer sees (after Dual rewrite when Dual is outermost)."""

    def __init__(self, app: ASGIApp, api_key: str, seen: list[str]) -> None:
        super().__init__(app, api_key=api_key)
        self._seen = seen

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        self._seen.append(request.url.path)
        return await super().dispatch(request, call_next)


def _stack(*, seen: list[str] | None = None) -> DualMcpSurfaceMiddleware:
    """Mirror production wiring: Dual outermost ASGI, Bearer via add_middleware."""
    inner = FastAPI()

    @inner.post("/mcp")
    async def mcp_ok() -> dict[str, bool]:
        return {"ok": True}

    @inner.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if seen is not None:
        inner.add_middleware(_RecordingBearer, api_key=API_KEY, seen=seen)
    else:
        inner.add_middleware(BearerAuthMiddleware, api_key=API_KEY)

    return DualMcpSurfaceMiddleware(inner, enabled=True)


def test_mcp_without_bearer_returns_401() -> None:
    client = TestClient(_stack())
    assert client.post("/mcp").status_code == 401


def test_mcp_v2_without_bearer_returns_401() -> None:
    client = TestClient(_stack())
    assert client.post("/mcp/v2").status_code == 401


def test_mcp_with_bearer_returns_200() -> None:
    client = TestClient(_stack())
    r = client.post("/mcp", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_mcp_v2_with_bearer_returns_200() -> None:
    client = TestClient(_stack())
    r = client.post("/mcp/v2", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_bearer_sees_rewritten_path_on_mcp_v2() -> None:
    seen: list[str] = []
    client = TestClient(_stack(seen=seen))
    client.post("/mcp/v2", headers={"Authorization": f"Bearer {API_KEY}"})
    assert seen == ["/mcp"]


def test_health_skips_bearer() -> None:
    client = TestClient(_stack())
    assert client.get("/health").status_code == 200
