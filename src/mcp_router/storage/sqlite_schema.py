"""Schema DDL and vec table bootstrap for SqliteStore."""

from __future__ import annotations

import contextlib
import sqlite3

SCHEMA_SCRIPT = """
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


def init_schema(conn: sqlite3.Connection, *, vec_enabled: bool) -> None:
    conn.executescript(SCHEMA_SCRIPT)
    if vec_enabled:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("SELECT 1 FROM tools_vec LIMIT 1")
    conn.commit()


def ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    try:
        conn.execute("SELECT 1 FROM tools_vec LIMIT 1")
        return
    except sqlite3.OperationalError:
        pass
    conn.execute(
        f"CREATE VIRTUAL TABLE tools_vec USING vec0(tool_id TEXT PRIMARY KEY, embedding float[{dim}])"
    )
