"""Poll registry.json for changes and invoke an async reload callback."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

Fingerprint = tuple[int, int]  # (mtime_ns, size)


def registry_fingerprint(path: Path) -> Fingerprint | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class RegistryHotReloader:
    """Background poller: mtime/size change → ``on_change`` (partial reindex path)."""

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], Awaitable[None]],
        *,
        interval_sec: float = 2.0,
        enabled: bool = True,
    ) -> None:
        self.path = path
        self.on_change = on_change
        self.interval_sec = max(0.1, interval_sec)
        self.enabled = enabled
        self._last: Fingerprint | None = registry_fingerprint(path)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._reload_lock = asyncio.Lock()

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="registry-hot-reload")
        logger.info(
            "Registry hot-reload watching %s (poll=%.1fs)",
            self.path,
            self.interval_sec,
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def poll_once(self) -> bool:
        """Check fingerprint once; return True if reload ran. Useful in tests."""
        if not self.enabled:
            return False
        current = registry_fingerprint(self.path)
        if current == self._last:
            return False
        self._last = current
        if current is None:
            logger.warning("Registry disappeared: %s", self.path)
            return False
        await self._safe_reload()
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
                break
            except TimeoutError:
                pass
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Registry hot-reload poll failed")

    async def _safe_reload(self) -> None:
        async with self._reload_lock:
            logger.info("Registry change detected at %s — reloading", self.path)
            await self.on_change()
