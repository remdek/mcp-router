"""Runtime application state (FastAPI DI + MCP tool access)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from mcp_router.config import Settings, get_settings
from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.index.indexer import ToolIndexer
from mcp_router.index.search import ToolSearch
from mcp_router.models.tools import IndexStatus
from mcp_router.services.aliases import AliasCatalog, load_aliases
from mcp_router.services.cache_service import CacheService
from mcp_router.services.failure_backoff import FailureBackoffTracker
from mcp_router.services.feedback import FeedbackService
from mcp_router.services.health_probe import DownstreamHealthProber
from mcp_router.services.search_telemetry import SearchTelemetry
from mcp_router.services.server_manager import ServerManager
from mcp_router.services.strict_policy import StrictPolicy, build_strict_policy
from mcp_router.storage.sqlite_store import SqliteStore


@dataclass
class AppState:
    """Holds process-wide router runtime objects (replaces module-level globals)."""

    settings: Settings
    cache_service: CacheService
    server_manager: ServerManager
    store: SqliteStore
    alias_catalog: AliasCatalog
    search_telemetry: SearchTelemetry
    feedback_service: FeedbackService
    health_prober: DownstreamHealthProber
    failure_backoff: FailureBackoffTracker
    strict_policy: StrictPolicy
    embedding_client: EmbeddingProvider | None = None
    tool_search: ToolSearch | None = None
    indexer: ToolIndexer | None = None
    index_status: IndexStatus = field(default_factory=IndexStatus)
    reindex_task: asyncio.Task[None] | None = None


def build_app_state(settings: Settings | None = None) -> AppState:
    cfg = settings or get_settings()
    cache = CacheService(ttl_seconds=cfg.cache_ttl_seconds)
    manager = ServerManager(cache_service=cache)
    store = SqliteStore(cfg.index_db_path)
    return AppState(
        settings=cfg,
        cache_service=cache,
        server_manager=manager,
        store=store,
        alias_catalog=load_aliases(cfg.aliases_path),
        search_telemetry=SearchTelemetry(store, outcome_window_sec=cfg.search_outcome_window_sec),
        feedback_service=FeedbackService(store),
        health_prober=DownstreamHealthProber(
            manager,
            interval_sec=cfg.health_probe_interval_sec,
            timeout_sec=cfg.health_probe_timeout_sec,
            concurrency=cfg.health_probe_concurrency,
            enabled=cfg.health_probe_enabled,
        ),
        failure_backoff=FailureBackoffTracker(
            backoff_sec=cfg.failure_backoff_sec,
            agent_reason_chars=cfg.failure_backoff_reason_chars,
            enabled=cfg.failure_backoff_enabled,
        ),
        strict_policy=build_strict_policy(
            allowlist=cfg.strict_allowlist,
            read_only=cfg.strict_read_only,
        ),
    )
