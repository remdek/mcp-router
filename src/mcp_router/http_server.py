"""FastAPI HTTP server with MCP Streamable HTTP endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_router.app_state import AppState, build_app_state
from mcp_router.embedding.factory import create_embedding_client
from mcp_router.http_mcp_tools import register_mcp_tools
from mcp_router.http_middleware import (
    BearerAuthMiddleware,
    CallerIdMiddleware,
    DualMcpSurfaceMiddleware,
)
from mcp_router.index.indexer import ToolIndexer
from mcp_router.index.search import ToolSearch
from mcp_router.models.tools import IndexStatus
from mcp_router.reap import start_zombie_reaper, stop_zombie_reaper
from mcp_router.services.registry_hot_reload import RegistryHotReloader
from mcp_router.services.registry_loader import load_registry
from mcp_router.services.strict_policy import (
    build_strict_policy,
    get_surface,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Single AppState object (MCP tools + composition root); FastAPI uses Depends(get_app_state).
state: AppState = build_app_state()
_registry_reloader: RegistryHotReloader | None = None

mcp = FastMCP(
    "mcp-router",
    json_response=True,
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
register_mcp_tools(mcp)


def get_app_state(request: Request) -> AppState:
    """FastAPI dependency — prefers app.state, falls back to module AppState."""
    app_state = getattr(request.app.state, "app_state", None)
    return app_state if isinstance(app_state, AppState) else state


def _load_registry():
    return load_registry(state.settings.registry_path)


async def _run_reindex() -> None:
    if state.indexer is None:
        return
    try:
        state.index_status = IndexStatus(status="indexing", tool_count=state.store.tool_count())
        result = await state.indexer.rebuild_index()
        state.index_status = IndexStatus.model_validate(result)
    except Exception as exc:
        logger.exception("Reindex failed: %s", exc)
        state.index_status = IndexStatus(
            status="error",
            error=str(exc),
            tool_count=state.store.tool_count(),
        )


def _schedule_reindex() -> None:
    if state.reindex_task and not state.reindex_task.done():
        return
    state.reindex_task = asyncio.create_task(_run_reindex())


async def _apply_registry_reload() -> None:
    """Reload registry.json into ServerManager and schedule a reindex (no restart)."""
    registry = _load_registry()
    changed = await state.server_manager.apply_registry_update(registry)
    state.alias_catalog.reload()
    registry_profile = None
    if registry.strict_profile is not None:
        registry_profile = registry.strict_profile.model_dump(by_alias=True)
    state.strict_policy = build_strict_policy(
        allowlist=state.settings.strict_allowlist,
        read_only=state.settings.strict_read_only,
        registry_profile=registry_profile,
    )
    logger.info(
        "Registry hot-reload applied (changed=%s, servers=%d)",
        changed or "none",
        len(state.server_manager.list_servers()),
    )
    if changed:
        _schedule_reindex()


async def _router_status_snapshot(app_state: AppState | None = None) -> dict[str, Any]:
    s = app_state or state
    emb_ok = False
    if s.embedding_client:
        emb_ok = await s.embedding_client.health_check()
    reindexing = s.index_status.status == "indexing" or (
        s.reindex_task is not None and not s.reindex_task.done()
    )
    quality: dict[str, Any] | None = None
    try:
        quality = s.search_telemetry.quality_metrics(days=7)
    except Exception:
        quality = None
    return {
        "router_ok": True,
        "index_status": s.index_status.model_dump(),
        "reindex_in_progress": reindexing,
        "tool_count": s.store.tool_count(),
        "last_indexed_at": s.store.get_meta("last_indexed_at"),
        "embedding_mode": s.settings.embedding_mode,
        "embedding_ok": emb_ok,
        "registered_servers": len(s.server_manager.list_servers()),
        "downstream_health": s.health_prober.snapshot(),
        "failure_backoff": s.failure_backoff.snapshot(),
        "shadow_ranker": s.settings.shadow_ranker,
        "search_quality": quality,
        "mcp_surfaces": {
            "default": "/mcp",
            "strict": "/mcp/v2" if s.settings.strict_mcp_enabled else None,
            "active": get_surface(),
            "strict_policy": s.strict_policy.to_public_dict(),
        },
        "reconnect_hint": (
            "On ECONNREFUSED or fetch failed: verify container is running, "
            "check GET /health, then reconnect MCP in Cursor."
        ),
    }


def _lookup_tool_schema(server_name: str, tool_name: str) -> dict[str, Any] | None:
    try:
        state.store.connect()
        row = state.store.get_tool(server_name, tool_name)
    except Exception:
        return None
    if not row:
        return None
    schema = row.get("tool_schema")
    if isinstance(schema, str):
        try:
            parsed: Any = json.loads(schema)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(schema, dict):
        return schema
    return None


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _registry_reloader

    logger.info("Starting mcp-router...")
    app.state.app_state = state

    registry = _load_registry()
    state.server_manager.load_registry(registry)
    state.alias_catalog.reload()
    registry_profile = None
    if registry.strict_profile is not None:
        registry_profile = registry.strict_profile.model_dump(by_alias=True)
    state.strict_policy = build_strict_policy(
        allowlist=state.settings.strict_allowlist,
        read_only=state.settings.strict_read_only,
        registry_profile=registry_profile,
    )
    logger.info(
        "MCP surfaces: /mcp (default); /mcp/v2 strict=%s policy=%s",
        state.settings.strict_mcp_enabled,
        state.strict_policy.to_public_dict(),
    )

    state.store.connect()
    if state.settings.embedding_available:
        state.embedding_client = create_embedding_client(state.settings)

    state.indexer = ToolIndexer(
        store=state.store,
        server_manager=state.server_manager,
        embedding_client=state.embedding_client,
        timeout_sec=state.settings.index_tools_timeout_sec,
        concurrency=state.settings.index_concurrency,
        aliases=state.alias_catalog,
    )
    state.tool_search = ToolSearch(
        store=state.store,
        server_manager=state.server_manager,
        embedding_client=state.embedding_client,
        rrf_k=state.settings.rrf_k,
        server_boost=state.settings.server_boost,
        aliases=state.alias_catalog,
        alias_boost=state.settings.alias_boost,
        shadow_ranker=state.settings.shadow_ranker,
    )

    if state.settings.reindex_on_start:
        if state.settings.reindex_background:
            _schedule_reindex()
        else:
            await _run_reindex()

    state.health_prober.start()
    start_zombie_reaper()

    _registry_reloader = RegistryHotReloader(
        state.settings.registry_path,
        _apply_registry_reload,
        interval_sec=state.settings.registry_poll_interval_sec,
        enabled=state.settings.registry_hot_reload,
    )
    _registry_reloader.start()

    async with mcp.session_manager.run():
        yield

    if _registry_reloader is not None:
        await _registry_reloader.stop()
        _registry_reloader = None
    await state.health_prober.stop()
    await stop_zombie_reaper()
    if state.embedding_client:
        await state.embedding_client.close()
    await state.server_manager.cleanup_all()
    state.store.close()


fastapi_app = FastAPI(title="mcp-router", version="0.1.0", lifespan=app_lifespan)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=state.settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
fastapi_app.add_middleware(CallerIdMiddleware)
if state.settings.router_api_key:
    fastapi_app.add_middleware(BearerAuthMiddleware, api_key=state.settings.router_api_key)

for route in reversed(mcp.streamable_http_app().routes):
    fastapi_app.router.routes.insert(0, route)


@fastapi_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mcp-router"}


@fastapi_app.get("/status")
async def status(app_state: AppState = Depends(get_app_state)) -> dict[str, Any]:
    return await _router_status_snapshot(app_state)


@fastapi_app.post("/admin/reindex")
async def admin_reindex(_app_state: AppState = Depends(get_app_state)) -> dict[str, str]:
    _schedule_reindex()
    return {"status": "scheduled"}


# Outermost ASGI (must wrap FastAPI middleware stack): rewrite /mcp/v2 → /mcp
# before BearerAuthMiddleware so auth sees the canonical /mcp path (WOR-834).
app = DualMcpSurfaceMiddleware(fastapi_app, enabled=state.settings.strict_mcp_enabled)


def create_app() -> DualMcpSurfaceMiddleware:
    return app
