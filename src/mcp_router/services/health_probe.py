"""Periodic downstream MCP health probes (tools/list ping)."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mcp_router.services.server_manager import ServerManager

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ServerProbeResult:
    server_name: str
    healthy: bool
    error: str | None = None
    latency_ms: float | None = None
    checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "healthy": self.healthy,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }


@dataclass
class DownstreamHealthState:
    last_probe_at: str | None = None
    probe_in_progress: bool = False
    results: dict[str, ServerProbeResult] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        healthy = sorted(name for name, r in self.results.items() if r.healthy)
        failed = sorted(name for name, r in self.results.items() if not r.healthy)
        return {
            "last_probe_at": self.last_probe_at,
            "probe_in_progress": self.probe_in_progress,
            "healthy_servers": healthy,
            "failed_servers": failed,
            "servers": {name: r.to_dict() for name, r in sorted(self.results.items())},
        }


class DownstreamHealthProber:
    """Background prober: network servers use one-shot transports; stdio shares call-path lock."""

    def __init__(
        self,
        server_manager: ServerManager,
        *,
        interval_sec: float = 60.0,
        timeout_sec: float = 15.0,
        concurrency: int = 4,
        enabled: bool = True,
    ) -> None:
        self.server_manager = server_manager
        self.interval_sec = max(1.0, interval_sec)
        self.timeout_sec = max(0.1, timeout_sec)
        self.concurrency = max(1, concurrency)
        self.enabled = enabled
        self._state = DownstreamHealthState()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def snapshot(self) -> dict[str, Any]:
        """Non-blocking read of last probe results (safe for hot-path status)."""
        return self._state.snapshot()

    async def probe_once(self) -> DownstreamHealthState:
        async with self._lock:
            self._state.probe_in_progress = True
        try:
            names = [s.name for s in self.server_manager.list_servers()]
            results: dict[str, ServerProbeResult] = {}
            if names:
                semaphore = asyncio.Semaphore(self.concurrency)

                async def probe_one(name: str) -> None:
                    async with semaphore:
                        results[name] = await self._probe_server(name)

                await asyncio.gather(*(probe_one(n) for n in names))

            async with self._lock:
                self._state.results = results
                self._state.last_probe_at = _utc_now_iso()
                self._state.probe_in_progress = False
            failed = [n for n, r in results.items() if not r.healthy]
            if failed:
                logger.warning("Downstream health probe failed: %s", ", ".join(sorted(failed)))
            else:
                logger.info("Downstream health probe ok (%s servers)", len(results))
            return self._state
        except Exception:
            async with self._lock:
                self._state.probe_in_progress = False
            raise

    async def _probe_server(self, server_name: str) -> ServerProbeResult:
        started = time.perf_counter()
        checked_at = _utc_now_iso()
        try:
            await asyncio.wait_for(
                self.server_manager.probe_server(server_name),
                timeout=self.timeout_sec,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            return ServerProbeResult(
                server_name=server_name,
                healthy=True,
                latency_ms=round(latency_ms, 2),
                checked_at=checked_at,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            return ServerProbeResult(
                server_name=server_name,
                healthy=False,
                error=str(exc),
                latency_ms=round(latency_ms, 2),
                checked_at=checked_at,
            )

    def start(self) -> None:
        if not self.enabled:
            logger.info("Downstream health probes disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="downstream-health-probe")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run_loop(self) -> None:
        logger.info(
            "Starting downstream health probes (interval=%ss timeout=%ss concurrency=%s)",
            self.interval_sec,
            self.timeout_sec,
            self.concurrency,
        )
        while not self._stop.is_set():
            try:
                await self.probe_once()
            except Exception as exc:
                logger.exception("Downstream health probe cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
            except TimeoutError:
                continue
