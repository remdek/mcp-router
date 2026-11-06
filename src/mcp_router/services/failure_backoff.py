"""Agent-visible short failure backoff (WOR-1539 / pi-mcp failure-backoff).

After a failed connect/call, keep a short window (~60s) with a sanitized reason.
Agents see ``failed Ns ago: <reason>`` on blocked calls / search / status.
This is the fast layer beside ``health_quarantine`` (probe policy).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp_router.services.responses import sanitize_error_message

DEFAULT_BACKOFF_SEC = 60.0
# Agents see a short reason on every blocked call (pi: 300 chars).
DEFAULT_AGENT_REASON_CHARS = 300
# Internal store can keep a longer diagnostic (pi: 8 KiB).
_STORED_REASON_CHARS = 8 * 1024


def truncate_reason(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    if max_chars == 1:
        return "…"
    cut = cleaned[: max_chars - 1].rstrip()
    space = cut.rfind(" ")
    if space > max_chars // 2:
        cut = cut[:space].rstrip()
    return f"{cut}…"


@dataclass(frozen=True)
class FailureRecord:
    failed_at: float
    reason: str
    failure_kind: str | None = None


class FailureBackoffTracker:
    """Per-server failure timestamps + reasons (process-local, lazy expiry)."""

    def __init__(
        self,
        *,
        backoff_sec: float = DEFAULT_BACKOFF_SEC,
        agent_reason_chars: int = DEFAULT_AGENT_REASON_CHARS,
        enabled: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.backoff_sec = float(backoff_sec)
        self.agent_reason_chars = int(agent_reason_chars)
        self.enabled = bool(enabled)
        self._clock = clock or time.monotonic
        self._records: dict[str, FailureRecord] = {}

    def record(
        self,
        server_name: str,
        reason: str,
        *,
        failure_kind: str | None = None,
    ) -> None:
        if not self.enabled or not server_name:
            return
        stored = truncate_reason(sanitize_error_message(reason), _STORED_REASON_CHARS)
        self._records[server_name] = FailureRecord(
            failed_at=self._clock(),
            reason=stored or "unknown error",
            failure_kind=failure_kind,
        )

    def clear(self, server_name: str) -> bool:
        return self._records.pop(server_name, None) is not None

    def _active_record(self, server_name: str) -> FailureRecord | None:
        if not self.enabled:
            return None
        rec = self._records.get(server_name)
        if rec is None:
            return None
        age = self._clock() - rec.failed_at
        if age > self.backoff_sec:
            self._records.pop(server_name, None)
            return None
        return rec

    def age_seconds(self, server_name: str) -> int | None:
        rec = self._active_record(server_name)
        if rec is None:
            return None
        return max(0, round(self._clock() - rec.failed_at))

    def describe(self, server_name: str) -> str | None:
        """``failed Ns ago: <reason>`` or None outside the window."""
        age = self.age_seconds(server_name)
        if age is None:
            return None
        rec = self._records.get(server_name)
        if rec is None:
            return f"failed {age}s ago"
        reason = truncate_reason(rec.reason, self.agent_reason_chars)
        return f"failed {age}s ago: {reason}" if reason else f"failed {age}s ago"

    def is_active(self, server_name: str) -> bool:
        return self._active_record(server_name) is not None

    def active_servers(self) -> set[str]:
        if not self.enabled:
            return set()
        return {name for name in list(self._records) if self.is_active(name)}

    def snapshot(self) -> dict[str, Any]:
        """Compact status for get_router_status / search envelopes."""
        servers: dict[str, Any] = {}
        for name in list(self._records):
            desc = self.describe(name)
            if desc is None:
                continue
            rec = self._records[name]
            servers[name] = {
                "age_sec": self.age_seconds(name),
                "message": desc,
                "failure_kind": rec.failure_kind,
            }
        return {
            "enabled": self.enabled,
            "backoff_sec": self.backoff_sec,
            "active": sorted(servers),
            "servers": servers,
        }


def annotate_server_results(
    results: list[dict[str, Any]],
    tracker: FailureBackoffTracker | None,
) -> list[dict[str, Any]]:
    """Attach ``failure_backoff`` / day_path markers for active backoff servers."""
    if tracker is None or not tracker.enabled:
        return results
    annotated: list[dict[str, Any]] = []
    for item in results:
        row = dict(item)
        name = str(row.get("server_name", ""))
        desc = tracker.describe(name) if name else None
        if desc:
            row["failure_backoff"] = desc
            # Quarantine may already set day_path=skip; keep that stronger mark.
            if row.get("day_path") != "skip":
                row["day_path"] = "backoff"
                row["healthy"] = False
        annotated.append(row)
    return annotated


def filter_tool_results(
    results: list[dict[str, Any]],
    backoff_servers: set[str],
) -> list[dict[str, Any]]:
    if not backoff_servers:
        return results
    return [r for r in results if str(r.get("server_name", "")) not in backoff_servers]
