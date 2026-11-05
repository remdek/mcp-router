"""Simple in-memory cache with TTL."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        value, ts = entry
        if time.time() - ts > self.ttl_seconds:
            del self._cache[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (value, time.time())

    def cleanup_all(self) -> None:
        self._cache.clear()
