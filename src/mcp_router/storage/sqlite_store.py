"""SQLite storage with FTS5 and sqlite-vec."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_router.storage.sqlite_audit import SqliteAuditMixin
from mcp_router.storage.sqlite_helpers import (
    _VEC_AVAILABLE,
    BRUTE_FORCE_VECTOR_SCAN_LIMIT,
    pack_embedding,
    with_lock,
)
from mcp_router.storage.sqlite_schema import ensure_vec_table, init_schema
from mcp_router.storage.sqlite_search import SqliteSearchMixin

# Re-export for tests that monkeypatch / import the public module.
__all__ = ["BRUTE_FORCE_VECTOR_SCAN_LIMIT", "SqliteStore"]

if _VEC_AVAILABLE:
    import sqlite_vec
else:
    sqlite_vec = None  # type: ignore


class SqliteStore(SqliteSearchMixin, SqliteAuditMixin):
    """Tool index backed by SQLite + FTS5 + optional sqlite-vec.

    One connection is shared for the process lifetime. Public methods take
    ``self._lock`` so concurrent callers (event-loop + worker threads) cannot
    hit ``sqlite3.ProgrammingError`` / interleaved writes.
    # lean: global lock; per-connection pool if throughput matters
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._vec_enabled = False
        self._embedding_dim: int | None = None

    @with_lock
    def connect(self) -> None:
        if self._conn:
            return
        # check_same_thread=False: connection may be used from worker threads;
        # self._lock serializes all operations on it.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if _VEC_AVAILABLE and sqlite_vec is not None:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._vec_enabled = True
        self._init_schema()

    @with_lock
    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        assert self._conn
        init_schema(self._conn, vec_enabled=self._vec_enabled)

    def _ensure_vec_table(self, dim: int) -> None:
        assert self._conn
        if not self._vec_enabled:
            return
        ensure_vec_table(self._conn, dim)
        self._embedding_dim = dim

    @with_lock
    def clear_index(self) -> None:
        assert self._conn
        self._conn.execute("DELETE FROM tools")
        self._conn.execute("DELETE FROM tools_fts")
        if self._vec_enabled:
            with contextlib.suppress(Exception):
                self._conn.execute("DROP TABLE IF EXISTS tools_vec")
        self._conn.commit()
        self._embedding_dim = None

    @with_lock
    def upsert_tools(
        self, tools: list[dict[str, Any]], embeddings: list[list[float]] | None = None
    ) -> int:
        assert self._conn
        now = datetime.now(UTC).isoformat()
        if embeddings and self._vec_enabled:
            self._ensure_vec_table(len(embeddings[0]))

        count = 0
        for i, tool in enumerate(tools):
            emb_blob = None
            if embeddings and i < len(embeddings):
                emb_blob = pack_embedding(embeddings[i])
            self._conn.execute(
                """
                INSERT OR REPLACE INTO tools
                (tool_id, server_name, tool_name, description, text_content, parameters, tool_schema, embedding, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool["tool_id"],
                    tool["server_name"],
                    tool["tool_name"],
                    tool.get("description", ""),
                    tool["text_content"],
                    json.dumps(tool.get("parameters", [])),
                    json.dumps(tool.get("tool_schema", {})),
                    emb_blob,
                    now,
                ),
            )
            self._conn.execute("DELETE FROM tools_fts WHERE tool_id = ?", (tool["tool_id"],))
            self._conn.execute(
                "INSERT INTO tools_fts (tool_id, server_name, tool_name, description, text_content) VALUES (?, ?, ?, ?, ?)",
                (
                    tool["tool_id"],
                    tool["server_name"],
                    tool["tool_name"],
                    tool.get("description", ""),
                    tool["text_content"],
                ),
            )
            if embeddings and self._vec_enabled and i < len(embeddings):
                self._conn.execute(
                    "INSERT OR REPLACE INTO tools_vec (tool_id, embedding) VALUES (?, ?)",
                    (tool["tool_id"], pack_embedding(embeddings[i])),
                )
            count += 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed_at', ?)",
            (now,),
        )
        self._conn.commit()
        return count

    @with_lock
    def tool_count(self) -> int:
        assert self._conn
        row = self._conn.execute("SELECT COUNT(*) AS c FROM tools").fetchone()
        return int(row["c"]) if row else 0

    @with_lock
    def get_meta(self, key: str) -> str | None:
        assert self._conn
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @with_lock
    def get_tool(self, server_name: str, tool_name: str) -> dict[str, Any] | None:
        assert self._conn
        tool_id = f"{server_name}:{tool_name}"
        row = self._conn.execute("SELECT * FROM tools WHERE tool_id = ?", (tool_id,)).fetchone()
        return dict(row) if row else None

    @with_lock
    def list_all_tools(self) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT tool_id, server_name, tool_name, description FROM tools"
        ).fetchall()
        return [dict(r) for r in rows]

    @with_lock
    def list_tools_with_schemas(self) -> list[dict[str, Any]]:
        """All indexed tools including ``tool_schema`` JSON (for tool-tax / ops)."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT tool_id, server_name, tool_name, description, tool_schema FROM tools"
        ).fetchall()
        return [dict(r) for r in rows]
