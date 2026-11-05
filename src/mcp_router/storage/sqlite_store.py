"""SQLite storage with FTS5 and sqlite-vec."""

import contextlib
import json
import logging
import math
import sqlite3
import struct
import threading
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cap brute-force cosine when sqlite-vec is unavailable / fails.
# Keeps latency bounded on large indexes; prefer sqlite-vec for ANN.
BRUTE_FORCE_VECTOR_SCAN_LIMIT = 5000

_VEC_AVAILABLE = False
try:
    import sqlite_vec

    _VEC_AVAILABLE = True
except ImportError:
    sqlite_vec = None  # type: ignore


def _with_lock(method):
    """Serialize access to the shared sqlite3.Connection across threads."""

    @wraps(method)
    def wrapper(self: "SqliteStore", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class SqliteStore:
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

    @_with_lock
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

    @_with_lock
    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _init_schema(self) -> None:
        assert self._conn
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            CREATE TABLE IF NOT EXISTS tools (
              tool_id TEXT PRIMARY KEY,
              server_name TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              description TEXT,
              text_content TEXT NOT NULL,
              parameters TEXT,
              tool_schema TEXT,
              embedding BLOB,
              indexed_at TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
              tool_id UNINDEXED,
              server_name,
              tool_name,
              description,
              text_content,
              tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS search_log (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              kind TEXT NOT NULL,
              query TEXT NOT NULL,
              results_json TEXT NOT NULL,
              caller_id TEXT,
              latency_ms REAL,
              shadow_results_json TEXT,
              page_diff INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_search_log_created
              ON search_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_search_log_caller
              ON search_log(caller_id, created_at);
            CREATE TABLE IF NOT EXISTS search_outcomes (
              id TEXT PRIMARY KEY,
              search_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              server_name TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              outcome TEXT NOT NULL,
              FOREIGN KEY (search_id) REFERENCES search_log(id)
            );
            CREATE INDEX IF NOT EXISTS idx_search_outcomes_search
              ON search_outcomes(search_id);
            CREATE TABLE IF NOT EXISTS search_misses (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              query TEXT NOT NULL,
              called_server TEXT NOT NULL,
              called_tool TEXT NOT NULL,
              search_id TEXT
            );
            CREATE TABLE IF NOT EXISTS call_audit (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              caller_id TEXT,
              server_name TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              search_id TEXT,
              ok INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_call_audit_created
              ON call_audit(created_at);
            CREATE INDEX IF NOT EXISTS idx_call_audit_caller
              ON call_audit(caller_id, created_at);
            CREATE TABLE IF NOT EXISTS reviews (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              caller_id TEXT,
              target_kind TEXT NOT NULL,
              target_id TEXT NOT NULL,
              usefulness TEXT NOT NULL,
              reason TEXT,
              UNIQUE(target_kind, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_target
              ON reviews(target_kind, target_id);
            CREATE TABLE IF NOT EXISTS feedback (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              caller_id TEXT,
              category TEXT NOT NULL,
              message TEXT NOT NULL,
              call_ids_json TEXT,
              search_ids_json TEXT,
              verified_call_ids_json TEXT,
              verified_search_ids_json TEXT
            );
            """
        )
        if self._vec_enabled:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute("SELECT 1 FROM tools_vec LIMIT 1")
        self._conn.commit()

    def _ensure_vec_table(self, dim: int) -> None:
        assert self._conn
        if not self._vec_enabled:
            return
        try:
            self._conn.execute("SELECT 1 FROM tools_vec LIMIT 1")
            return
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            f"CREATE VIRTUAL TABLE tools_vec USING vec0(tool_id TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        self._embedding_dim = dim

    @_with_lock
    def clear_index(self) -> None:
        assert self._conn
        self._conn.execute("DELETE FROM tools")
        self._conn.execute("DELETE FROM tools_fts")
        if self._vec_enabled:
            with contextlib.suppress(Exception):
                self._conn.execute("DROP TABLE IF EXISTS tools_vec")
        self._conn.commit()
        self._embedding_dim = None

    @_with_lock
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
                emb_blob = _pack_embedding(embeddings[i])
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
                    (tool["tool_id"], _pack_embedding(embeddings[i])),
                )
            count += 1
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed_at', ?)",
            (now,),
        )
        self._conn.commit()
        return count

    @_with_lock
    def tool_count(self) -> int:
        assert self._conn
        row = self._conn.execute("SELECT COUNT(*) AS c FROM tools").fetchone()
        return int(row["c"]) if row else 0

    @_with_lock
    def get_meta(self, key: str) -> str | None:
        assert self._conn
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _fts_query(self, query: str) -> str:
        terms = []
        for part in query.split():
            token = part.strip()
            if not token:
                continue
            token = token.replace('"', '""')
            terms.append(f'"{token}"')
        return " OR ".join(terms)

    @_with_lock
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

    @_with_lock
    def search_vector(self, query_vec: list[float], limit: int) -> list[dict[str, Any]]:
        assert self._conn
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
                    (_pack_embedding(query_vec), limit),
                ).fetchall()
                return [{**dict(r), "score": 1.0 - float(r["dist"])} for r in rows]
            except sqlite3.OperationalError as exc:
                logger.debug("vec search failed, fallback to brute force: %s", exc)

        # Brute-force fallback (bounded — see BRUTE_FORCE_VECTOR_SCAN_LIMIT)
        total_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM tools WHERE embedding IS NOT NULL"
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0
        if total > BRUTE_FORCE_VECTOR_SCAN_LIMIT:
            logger.warning(
                "brute-force vector search capped at %d of %d tools with embeddings; "
                "install sqlite-vec for unbounded ANN search",
                BRUTE_FORCE_VECTOR_SCAN_LIMIT,
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
            (BRUTE_FORCE_VECTOR_SCAN_LIMIT,),
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if not row["embedding"]:
                continue
            dim = len(query_vec)
            vec = list(struct.unpack(f"{dim}f", row["embedding"][: dim * 4]))
            sim = _cosine_similarity(query_vec, vec)
            scored.append((sim, dict(row)))
        scored.sort(key=lambda x: -x[0])
        return [{**item, "score": score} for score, item in scored[:limit]]

    @_with_lock
    def get_tool(self, server_name: str, tool_name: str) -> dict[str, Any] | None:
        assert self._conn
        tool_id = f"{server_name}:{tool_name}"
        row = self._conn.execute("SELECT * FROM tools WHERE tool_id = ?", (tool_id,)).fetchone()
        return dict(row) if row else None

    @_with_lock
    def list_all_tools(self) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT tool_id, server_name, tool_name, description FROM tools"
        ).fetchall()
        return [dict(r) for r in rows]

    @_with_lock
    def insert_search_log(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO search_log
            (id, created_at, kind, query, results_json, caller_id, latency_ms,
             shadow_results_json, page_diff)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["created_at"],
                row["kind"],
                row["query"],
                json.dumps(row["results_json"], ensure_ascii=False),
                row.get("caller_id"),
                row.get("latency_ms"),
                json.dumps(row["shadow_results_json"], ensure_ascii=False)
                if row.get("shadow_results_json") is not None
                else None,
                row.get("page_diff"),
            ),
        )
        self._conn.commit()

    @_with_lock
    def insert_search_outcome(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO search_outcomes
            (id, search_id, created_at, server_name, tool_name, outcome)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["search_id"],
                row["created_at"],
                row["server_name"],
                row["tool_name"],
                row["outcome"],
            ),
        )
        self._conn.commit()

    @_with_lock
    def insert_search_miss(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO search_misses
            (id, created_at, query, called_server, called_tool, search_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["created_at"],
                row["query"],
                row["called_server"],
                row["called_tool"],
                row.get("search_id"),
            ),
        )
        self._conn.commit()

    @_with_lock
    def latest_search_for_caller(
        self, caller_id: str, *, kind: str = "tools", since: str | None = None
    ) -> dict[str, Any] | None:
        assert self._conn
        if since:
            row = self._conn.execute(
                """
                SELECT * FROM search_log
                WHERE caller_id = ? AND kind = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (caller_id, kind, since),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM search_log
                WHERE caller_id = ? AND kind = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (caller_id, kind),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["results_json"] = json.loads(data["results_json"] or "[]")
        if data.get("shadow_results_json"):
            data["shadow_results_json"] = json.loads(data["shadow_results_json"])
        return data

    @_with_lock
    def search_followed_by_call(self, search_id: str) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT server_name, tool_name, outcome, created_at
            FROM search_outcomes WHERE search_id = ?
            ORDER BY created_at
            """,
            (search_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_with_lock
    def get_search_log(self, search_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute("SELECT * FROM search_log WHERE id = ?", (search_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["results_json"] = json.loads(data["results_json"] or "[]")
        if data.get("shadow_results_json"):
            data["shadow_results_json"] = json.loads(data["shadow_results_json"])
        return data

    @_with_lock
    def insert_call_audit(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO call_audit
            (id, created_at, caller_id, server_name, tool_name, search_id, ok)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["created_at"],
                row.get("caller_id"),
                row["server_name"],
                row["tool_name"],
                row.get("search_id"),
                row.get("ok"),
            ),
        )
        self._conn.commit()

    @_with_lock
    def get_call_audit(self, call_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute("SELECT * FROM call_audit WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    @_with_lock
    def insert_review(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO reviews
            (id, created_at, caller_id, target_kind, target_id, usefulness, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["created_at"],
                row.get("caller_id"),
                row["target_kind"],
                row["target_id"],
                row["usefulness"],
                row.get("reason"),
            ),
        )
        self._conn.commit()

    @_with_lock
    def get_review(self, target_kind: str, target_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute(
            """
            SELECT * FROM reviews
            WHERE target_kind = ? AND target_id = ?
            """,
            (target_kind, target_id),
        ).fetchone()
        return dict(row) if row else None

    @_with_lock
    def insert_feedback(self, row: dict[str, Any]) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO feedback
            (id, created_at, caller_id, category, message,
             call_ids_json, search_ids_json,
             verified_call_ids_json, verified_search_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["created_at"],
                row.get("caller_id"),
                row["category"],
                row["message"],
                json.dumps(row.get("call_ids") or [], ensure_ascii=False),
                json.dumps(row.get("search_ids") or [], ensure_ascii=False),
                json.dumps(row.get("verified_call_ids") or [], ensure_ascii=False),
                json.dumps(row.get("verified_search_ids") or [], ensure_ascii=False),
            ),
        )
        self._conn.commit()

    @_with_lock
    def get_feedback(self, feedback_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in (
            "call_ids_json",
            "search_ids_json",
            "verified_call_ids_json",
            "verified_search_ids_json",
        ):
            short = key.removesuffix("_json")
            data[short] = json.loads(data.pop(key) or "[]")
        return data

    @_with_lock
    def reviews_for_search(self, search_id: str) -> list[dict[str, Any]]:
        """Reviews targeting this search or any call_audit row joined to it."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT r.* FROM reviews r
            WHERE (r.target_kind = 'search' AND r.target_id = ?)
               OR (r.target_kind = 'call' AND r.target_id IN (
                    SELECT id FROM call_audit WHERE search_id = ?
               ))
            ORDER BY r.created_at
            """,
            (search_id, search_id),
        ).fetchall()
        return [dict(r) for r in rows]

    @_with_lock
    def search_quality_metrics(self, *, days: int = 7) -> dict[str, Any]:
        assert self._conn
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        total = self._conn.execute(
            "SELECT COUNT(*) AS c FROM search_log WHERE kind = 'tools' AND created_at >= ?",
            (since,),
        ).fetchone()["c"]
        with_followup = self._conn.execute(
            """
            SELECT COUNT(DISTINCT s.id) AS c
            FROM search_log s
            JOIN search_outcomes o ON o.search_id = s.id
            WHERE s.kind = 'tools' AND s.created_at >= ?
            """,
            (since,),
        ).fetchone()["c"]
        latency = self._conn.execute(
            """
            SELECT AVG(latency_ms) AS avg_ms FROM search_log
            WHERE kind = 'tools' AND created_at >= ? AND latency_ms IS NOT NULL
            """,
            (since,),
        ).fetchone()["avg_ms"]
        shadow_rows = self._conn.execute(
            """
            SELECT COUNT(*) AS c FROM search_log
            WHERE kind = 'tools' AND created_at >= ? AND page_diff IS NOT NULL
            """,
            (since,),
        ).fetchone()["c"]
        shadow_diff = self._conn.execute(
            """
            SELECT COUNT(*) AS c FROM search_log
            WHERE kind = 'tools' AND created_at >= ? AND page_diff = 1
            """,
            (since,),
        ).fetchone()["c"]
        return {
            "days": days,
            "tool_searches": int(total),
            "with_followup_call": int(with_followup),
            "followup_rate": round(with_followup / total, 4) if total else 0.0,
            "avg_latency_ms": round(float(latency), 2) if latency is not None else None,
            "shadow_compared": int(shadow_rows),
            "page_diff_rate": round(shadow_diff / shadow_rows, 4) if shadow_rows else 0.0,
        }
