"""Search/call audit, feedback, and quality metrics for SqliteStore."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp_router.storage.sqlite_helpers import with_lock


class SqliteAuditMixin:
    _conn: sqlite3.Connection | None
    _lock: threading.Lock

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
    def get_call_audit(self, call_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute("SELECT * FROM call_audit WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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

    @with_lock
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
