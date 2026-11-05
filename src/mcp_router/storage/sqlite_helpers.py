"""Shared helpers for SqliteStore (lock, vectors, optional sqlite-vec)."""

from __future__ import annotations

import math
import struct
from functools import wraps
from typing import Any

# Cap brute-force cosine when sqlite-vec is unavailable / fails.
# Keeps latency bounded on large indexes; prefer sqlite-vec for ANN.
BRUTE_FORCE_VECTOR_SCAN_LIMIT = 5000

_VEC_AVAILABLE = False
try:
    import sqlite_vec

    _VEC_AVAILABLE = True
except ImportError:
    sqlite_vec = None  # type: ignore


def with_lock(method):
    """Serialize access to the shared sqlite3.Connection across threads."""

    @wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)
