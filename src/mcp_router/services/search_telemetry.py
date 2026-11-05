"""Search quality telemetry: SearchLog, call outcomes, metrics."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, cast

logger = logging.getLogger(__name__)

_caller_id: ContextVar[str] = ContextVar("mcp_router_caller_id", default="anonymous")

# Avoid logging obvious secrets if pasted into a search query.
_SECRETISH = re.compile(
    r"(?i)\b(bearer\s+[a-z0-9._\-]{8,}|sk-[a-z0-9]{16,}|ghp_[a-z0-9]{20,}|"
    r"api[_-]?key\s*[:=]\s*\S+)\b"
)


def set_caller_id(caller_id: str) -> None:
    _caller_id.set(caller_id or "anonymous")


def get_caller_id() -> str:
    return _caller_id.get()


def caller_id_from_headers(authorization: str | None, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:128]
    if authorization and authorization.strip():
        digest = hashlib.sha256(authorization.strip().encode("utf-8")).hexdigest()[:16]
        return f"auth:{digest}"
    return "anonymous"


def sanitize_query(query: str) -> str:
    cleaned = _SECRETISH.sub("[REDACTED]", query)
    return cleaned[:2000]


def compact_results(results: list[dict[str, Any]], *, kind: str = "tools") -> list[dict[str, Any]]:
    """Store only ids/ranks — never tool arguments or schemas."""
    page: list[dict[str, Any]] = []
    for rank, item in enumerate(results, start=1):
        if kind == "servers":
            page.append(
                {
                    "rank": rank,
                    "server_name": item.get("name") or item.get("server_name"),
                }
            )
        else:
            page.append(
                {
                    "rank": rank,
                    "tool_id": item.get("tool_id")
                    or f"{item.get('server_name')}:{item.get('tool_name')}",
                    "server_name": item.get("server_name"),
                    "tool_name": item.get("tool_name"),
                }
            )
    return page


def page_differs(baseline: list[dict[str, Any]], shadow: list[dict[str, Any]]) -> bool:
    left = [r.get("tool_id") or r.get("server_name") for r in baseline]
    right = [r.get("tool_id") or r.get("server_name") for r in shadow]
    return left != right


def classify_call_outcome(
    results_page: list[dict[str, Any]],
    server_name: str,
    tool_name: str,
) -> str:
    tool_id = f"{server_name}:{tool_name}"
    ids = {r.get("tool_id") for r in results_page if r.get("tool_id")}
    if tool_id in ids:
        return "hit"
    servers = {r.get("server_name") for r in results_page if r.get("server_name")}
    if server_name in servers:
        return "wrong_tool"
    return "miss"


class SearchTelemetry:
    """Fire-and-forget search/call logging against SqliteStore."""

    def __init__(
        self,
        store: Any,
        outcome_window_sec: int = 600,
        quality_metrics_ttl_sec: float = 45.0,
    ) -> None:
        self.store = store
        self.outcome_window_sec = outcome_window_sec
        self.quality_metrics_ttl_sec = quality_metrics_ttl_sec
        # (metrics, monotonic_ts, days) — avoids COUNT(*) on every /status
        self._quality_cache: tuple[dict[str, Any], float, int] | None = None

    def quality_metrics(self, *, days: int = 7) -> dict[str, Any]:
        """Return search_quality_metrics with a short TTL cache for /status."""
        now = time.monotonic()
        cached = self._quality_cache
        if (
            cached is not None
            and cached[2] == days
            and now - cached[1] < self.quality_metrics_ttl_sec
        ):
            return cached[0]
        self.store.connect()
        metrics = cast(dict[str, Any], self.store.search_quality_metrics(days=days))
        self._quality_cache = (metrics, now, days)
        return metrics

    def log_search(
        self,
        *,
        kind: str,
        query: str,
        results: list[dict[str, Any]],
        latency_ms: float,
        shadow_results: list[dict[str, Any]] | None = None,
        caller_id: str | None = None,
    ) -> str | None:
        try:
            self.store.connect()
            search_id = str(uuid.uuid4())
            page = compact_results(results, kind=kind)
            shadow_page = (
                compact_results(shadow_results, kind=kind) if shadow_results is not None else None
            )
            diff = (
                1
                if shadow_page is not None and page_differs(page, shadow_page)
                else (0 if shadow_page is not None else None)
            )
            self.store.insert_search_log(
                {
                    "id": search_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "kind": kind,
                    "query": sanitize_query(query),
                    "results_json": page,
                    "caller_id": caller_id or get_caller_id(),
                    "latency_ms": latency_ms,
                    "shadow_results_json": shadow_page,
                    "page_diff": diff,
                }
            )
            return search_id
        except Exception as exc:
            logger.warning("search log failed: %s", exc)
            return None

    def log_call_outcome(self, server_name: str, tool_name: str) -> str | None:
        try:
            self.store.connect()
            caller = get_caller_id()
            since = (datetime.now(UTC) - timedelta(seconds=self.outcome_window_sec)).isoformat()
            search = self.store.latest_search_for_caller(caller, kind="tools", since=since)
            if not search:
                return None
            outcome = classify_call_outcome(search["results_json"], server_name, tool_name)
            self.store.insert_search_outcome(
                {
                    "id": str(uuid.uuid4()),
                    "search_id": search["id"],
                    "created_at": datetime.now(UTC).isoformat(),
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "outcome": outcome,
                }
            )
            if outcome == "miss":
                self.store.insert_search_miss(
                    {
                        "id": str(uuid.uuid4()),
                        "created_at": datetime.now(UTC).isoformat(),
                        "query": search["query"],
                        "called_server": server_name,
                        "called_tool": tool_name,
                        "search_id": search["id"],
                    }
                )
            return outcome
        except Exception as exc:
            logger.warning("search outcome log failed: %s", exc)
            return None

    def log_call_audit(
        self,
        server_name: str,
        tool_name: str,
        *,
        ok: bool | None = None,
        caller_id: str | None = None,
    ) -> str | None:
        """Write call_audit row (for review join) and best-effort search outcome."""
        try:
            self.store.connect()
            caller = caller_id or get_caller_id()
            since = (datetime.now(UTC) - timedelta(seconds=self.outcome_window_sec)).isoformat()
            search = self.store.latest_search_for_caller(caller, kind="tools", since=since)
            call_id = str(uuid.uuid4())
            self.store.insert_call_audit(
                {
                    "id": call_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "caller_id": caller,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "search_id": search["id"] if search else None,
                    "ok": None if ok is None else (1 if ok else 0),
                }
            )
            # Outcome join still fire-and-forget style via same path.
            if search:
                outcome = classify_call_outcome(search["results_json"], server_name, tool_name)
                self.store.insert_search_outcome(
                    {
                        "id": str(uuid.uuid4()),
                        "search_id": search["id"],
                        "created_at": datetime.now(UTC).isoformat(),
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "outcome": outcome,
                    }
                )
                if outcome == "miss":
                    self.store.insert_search_miss(
                        {
                            "id": str(uuid.uuid4()),
                            "created_at": datetime.now(UTC).isoformat(),
                            "query": search["query"],
                            "called_server": server_name,
                            "called_tool": tool_name,
                            "search_id": search["id"],
                        }
                    )
            return call_id
        except Exception as exc:
            logger.warning("call audit log failed: %s", exc)
            return None
