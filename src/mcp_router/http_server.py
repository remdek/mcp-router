"""FastAPI HTTP server with MCP Streamable HTTP endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from mcp_router.app_state import AppState, build_app_state
from mcp_router.embedding.factory import create_embedding_client
from mcp_router.index.indexer import ToolIndexer
from mcp_router.index.search import ToolSearch
from mcp_router.models.tools import IndexStatus
from mcp_router.reap import start_zombie_reaper, stop_zombie_reaper
from mcp_router.services.feedback import (
    FeedbackValidationError,
    ReviewTargetNotFound,
)
from mcp_router.services.feedback_contract import (
    FEEDBACK_DESCRIPTION,
    REVIEW_DESCRIPTION,
    FeedbackCategory,
    ReviewUsefulness,
)
from mcp_router.services.health_quarantine import (
    annotate_server_results,
    failed_server_names,
    filter_tool_results,
    is_server_quarantined,
    probe_errors,
)
from mcp_router.services.registry_hot_reload import RegistryHotReloader
from mcp_router.services.registry_loader import load_registry
from mcp_router.services.responses import (
    assess_search_confidence,
    classify_failure_kind,
    group_results_by_server,
    normalize_tool_result,
    remediation_error,
)
from mcp_router.services.search_telemetry import (
    caller_id_from_headers,
    set_caller_id,
)
from mcp_router.services.server_search import search_servers as search_registered_servers
from mcp_router.services.strict_policy import (
    build_strict_policy,
    get_surface,
    set_surface,
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


@mcp.tool()
async def get_router_status() -> str:
    """Router health: index state, embedding mode, reindex progress, downstream probes, reconnect hints."""
    return json.dumps(await _router_status_snapshot(), ensure_ascii=False, indent=2)


@mcp.tool()
async def search_servers(query: str = "", limit: int = 20) -> str:
    """Search registered downstream MCP servers (not individual tools)."""
    started = time.perf_counter()
    results = search_registered_servers(state.server_manager, state.store, query=query, limit=limit)
    if get_surface() == "strict":
        results = state.strict_policy.filter_server_results(results)[:limit]
    if state.settings.health_quarantine_enabled:
        results = annotate_server_results(
            results,
            failed_server_names(state.health_prober),
            errors=probe_errors(state.health_prober),
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    search_id = state.search_telemetry.log_search(
        kind="servers",
        query=query,
        results=results,
        latency_ms=latency_ms,
    )
    payload = {
        "query": query,
        "search_id": search_id,
        "results": results,
        "router": await _router_status_snapshot(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_tools(
    query: str,
    limit: int = 10,
    vector_weight: float = 0.4,
    fulltext_weight: float = 0.6,
    max_per_server: int | None = None,
) -> str:
    """Search MCP tools by semantic or keyword query (RU/EN)."""
    if state.tool_search is None:
        return json.dumps(
            remediation_error(
                "index_degraded",
                error="search not initialized",
            ),
            ensure_ascii=False,
        )
    raw_limit = limit * 3
    started = time.perf_counter()
    results, shadow = await state.tool_search.search_with_shadow(
        query=query,
        limit=raw_limit,
        vector_weight=vector_weight,
        fulltext_weight=fulltext_weight,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    per_server = max_per_server or state.settings.search_max_per_server
    if state.settings.health_quarantine_enabled:
        quarantine = failed_server_names(state.health_prober)
        results = filter_tool_results(results, quarantine)
        if shadow is not None:
            shadow = filter_tool_results(shadow, quarantine)
    grouped = group_results_by_server(results, max_per_server=per_server)[:limit]
    if get_surface() == "strict":
        grouped = state.strict_policy.filter_tool_results(grouped)[:limit]
    shadow_grouped = None
    if shadow is not None:
        shadow_grouped = group_results_by_server(shadow, max_per_server=per_server)[:limit]
        if get_surface() == "strict":
            shadow_grouped = state.strict_policy.filter_tool_results(shadow_grouped)[:limit]
    search_id = state.search_telemetry.log_search(
        kind="tools",
        query=query,
        results=grouped,
        latency_ms=latency_ms,
        shadow_results=shadow_grouped,
    )
    confidence, confidence_hint = assess_search_confidence(grouped)
    router = await _router_status_snapshot()
    index_status_name = router["index_status"]["status"]
    index_degraded = (
        router["reindex_in_progress"]
        or index_status_name in ("error", "indexing", "pending")
        or (confidence == "none" and int(router["tool_count"] or 0) == 0)
    )
    if router["reindex_in_progress"]:
        confidence_hint = f"{confidence_hint} Index rebuild in progress — results may be stale."

    payload: dict[str, Any] = {
        "ok": True,
        "query": query,
        "search_id": search_id,
        "confidence": confidence,
        "confidence_hint": confidence_hint,
        "results": grouped,
        "router": {
            "reindex_in_progress": router["reindex_in_progress"],
            "index_status": index_status_name,
            "tool_count": router["tool_count"],
        },
    }
    if index_degraded:
        rem = remediation_error(
            "index_degraded",
            error=f"index_status={index_status_name}; tool_count={router['tool_count']}",
        )
        payload["failure_kind"] = rem["failure_kind"]
        payload["reconnect_hint"] = rem["reconnect_hint"]
        payload["next_action"] = rem["next_action"]

    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
async def call_server_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Call a tool on a downstream MCP server."""
    args = arguments or {}
    call_id = state.search_telemetry.log_call_audit(server_name, tool_name, ok=None)
    if get_surface() == "strict":
        schema = _lookup_tool_schema(server_name, tool_name)
        allowed, reason = state.strict_policy.allows_tool(
            server_name, tool_name, tool_schema=schema
        )
        if not allowed:
            return json.dumps(
                {
                    **remediation_error(
                        "strict_denied",
                        error=reason,
                        server_name=server_name,
                        tool_name=tool_name,
                    ),
                    "call_id": call_id,
                },
                ensure_ascii=False,
            )
    if state.server_manager.get_server_config(server_name) is None:
        return json.dumps(
            {
                **remediation_error(
                    "tool_not_found",
                    error=f"Server not found in registry: {server_name}",
                    server_name=server_name,
                    tool_name=tool_name,
                ),
                "call_id": call_id,
            },
            ensure_ascii=False,
        )
    quarantine = failed_server_names(state.health_prober)
    if is_server_quarantined(
        server_name,
        quarantine,
        enabled=state.settings.health_quarantine_enabled,
    ):
        err = probe_errors(state.health_prober).get(server_name) or "failed last health probe"
        return json.dumps(
            {
                **remediation_error(
                    "server_unhealthy",
                    error=f"Server quarantined (unhealthy): {server_name}: {err}",
                    server_name=server_name,
                    tool_name=tool_name,
                ),
                "call_id": call_id,
            },
            ensure_ascii=False,
        )
    try:
        raw = await state.server_manager.call_tool(server_name, tool_name, args)
        normalized = normalize_tool_result(raw)
        return json.dumps(
            {
                "ok": not normalized["is_error"],
                "call_id": call_id,
                "server_name": server_name,
                "tool_name": tool_name,
                "result": normalized,
                "error_source": "tool" if normalized["is_error"] else None,
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as exc:
        kind = classify_failure_kind(exc)
        return json.dumps(
            {
                **remediation_error(
                    kind,
                    error=str(exc),
                    server_name=server_name,
                    tool_name=tool_name,
                ),
                "call_id": call_id,
            },
            ensure_ascii=False,
        )


@mcp.tool(description=FEEDBACK_DESCRIPTION)
async def feedback(
    category: FeedbackCategory,
    message: str,
    call_ids: list[str] | None = None,
    search_ids: list[str] | None = None,
) -> str:
    """Local feedback intake (quality / friction / other). Does not call downstream."""
    try:
        result = state.feedback_service.submit_feedback(
            category=category,
            message=message,
            call_ids=call_ids,
            search_ids=search_ids,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except FeedbackValidationError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@mcp.tool(description=REVIEW_DESCRIPTION)
async def review(
    usefulness: ReviewUsefulness,
    call_id: str | None = None,
    search_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Rate a prior call or search. Local SQLite write only — no downstream."""
    try:
        result = state.feedback_service.submit_review(
            usefulness=usefulness,
            call_id=call_id,
            search_id=search_id,
            reason=reason,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except ReviewTargetNotFound as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except FeedbackValidationError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


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
