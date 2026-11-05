"""FTS5 and vector/ANN search methods for SqliteStore."""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
from typing import Any

from mcp_router.storage.sqlite_helpers import (
    cosine_similarity,
    pack_embedding,
    with_lock,
)

logger = logging.getLogger("mcp_router.storage.sqlite_store")


class SqliteSearchMixin:
    _conn: sqlite3.Connection | None
    _lock: threading.Lock
    _vec_enabled: bool
    _embedding_dim: int | None

    def _fts_query(self, query: str) -> str:
        terms = []
        for part in query.split():
            token = part.strip()
            if not token:
                continue
            token = token.replace('"', '""')
            terms.append(f'"{token}"')
        return " OR ".join(terms)

    @with_lock
    def search_fts(self, query: str, limit: int) -> list[dict[str, Any]]:
        assert self._conn
        safe = self._fts_query(query)
        if not safe:
            return []
        rows = self._conn.execute(
            """
            SELECT t.tool_id, t.server_name, t.tool_name, t.description, t.tool_schema,
                   bm25(tools_fts) AS score
            FROM tools_fts
            JOIN tools t ON t.tool_id = tools_fts.tool_id
            WHERE tools_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (safe, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @with_lock
    def search_vector(self, query_vec: list[float], limit: int) -> list[dict[str, Any]]:
        assert self._conn
        # Resolve cap via sqlite_store so tests can monkeypatch the public module.
        from mcp_router.storage import sqlite_store as store_mod

        scan_cap = store_mod.BRUTE_FORCE_VECTOR_SCAN_LIMIT

        if self._vec_enabled and self._embedding_dim:
            try:
                rows = self._conn.execute(
                    """
                    SELECT t.tool_id, t.server_name, t.tool_name, t.description, t.tool_schema,
                           vec_distance_cosine(v.embedding, ?) AS dist
                    FROM tools_vec v
                    JOIN tools t ON t.tool_id = v.tool_id
                    ORDER BY dist
                    LIMIT ?
                    """,
                    (pack_embedding(query_vec), limit),
                ).fetchall()
                return [{**dict(r), "score": 1.0 - float(r["dist"])} for r in rows]
            except sqlite3.OperationalError as exc:
                logger.debug("vec search failed, fallback to brute force: %s", exc)

        # Brute-force fallback (bounded — see BRUTE_FORCE_VECTOR_SCAN_LIMIT)
        total_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM tools WHERE embedding IS NOT NULL"
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0
        if total > scan_cap:
            logger.warning(
                "brute-force vector search capped at %d of %d tools with embeddings; "
                "install sqlite-vec for unbounded ANN search",
                scan_cap,
                total,
            )
        rows = self._conn.execute(
            """
            SELECT tool_id, server_name, tool_name, description, tool_schema, embedding
            FROM tools
            WHERE embedding IS NOT NULL
            ORDER BY tool_id
            LIMIT ?
            """,
            (scan_cap,),
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not row["embedding"]:
                continue
            dim = len(query_vec)
            vec = list(struct.unpack(f"{dim}f", row["embedding"][: dim * 4]))
            sim = cosine_similarity(query_vec, vec)
            scored.append((sim, dict(row)))
        scored.sort(key=lambda x: -x[0])
        return [{**item, "score": score} for score, item in scored[:limit]]
