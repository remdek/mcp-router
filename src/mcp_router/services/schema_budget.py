"""Schema-on-demand gating for search hits (WOR-1537 / ADR 0001).

Strong hits keep full ``tool_schema``; weak hits keep identity + description only.
pi-mcp-adapter uses ``top / 4`` on semantic classifier scores; RRF scores cluster
tightly, so we use a relative floor ``top * ratio`` (default 0.85) instead.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_STRONG_MATCH_RATIO = 0.85


def _hit_keys(item: dict[str, Any]) -> set[str]:
    server = str(item.get("server_name") or "")
    tool = str(item.get("tool_name") or "")
    tool_id = str(item.get("tool_id") or "")
    keys = {k for k in (tool_id, tool, f"{server}:{tool}") if k}
    return keys


def strong_match_floor(
    results: list[dict[str, Any]],
    *,
    ratio: float = DEFAULT_STRONG_MATCH_RATIO,
) -> float | None:
    """Minimum score that still counts as a strong match (inclusive)."""
    if not results:
        return None
    top = max(float(r.get("score") or 0.0) for r in results)
    if top <= 0:
        return 0.0
    safe = min(1.0, max(0.0, float(ratio)))
    return top * safe


def apply_schema_on_demand(
    results: list[dict[str, Any]],
    *,
    ratio: float = DEFAULT_STRONG_MATCH_RATIO,
    include_schemas: bool = False,
    include_schema_for: list[str] | None = None,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return copies of hits with ``tool_schema`` gated by strong-match floor.

    - ``include_schemas=True``: keep every schema (opt-out / debug).
    - ``include_schema_for``: force schema for matching tool_id / tool_name /
      ``server:tool`` tokens (case-sensitive as stored).
    - Weak hits: drop ``tool_schema``, set ``schema_omitted=True``.
    - Strong hits: keep ``tool_schema`` when present; set ``schema_omitted=False``.
    """
    if not results:
        return []
    if not enabled or include_schemas:
        out: list[dict[str, Any]] = []
        for item in results:
            copy = dict(item)
            copy["schema_omitted"] = "tool_schema" not in copy or copy.get("tool_schema") is None
            out.append(copy)
        return out

    force = {t.strip() for t in (include_schema_for or []) if t and t.strip()}
    floor = strong_match_floor(results, ratio=ratio)
    # Always treat the first row as strong so top-1 recall stays one-shot.
    top_id = id(results[0])

    shaped: list[dict[str, Any]] = []
    for item in results:
        copy = dict(item)
        keys = _hit_keys(copy)
        forced = bool(force and keys & force)
        score = float(copy.get("score") or 0.0)
        is_strong = forced or id(item) == top_id or (floor is not None and score >= floor)
        if is_strong and copy.get("tool_schema") is not None:
            copy["schema_omitted"] = False
        else:
            copy.pop("tool_schema", None)
            copy["schema_omitted"] = True
        shaped.append(copy)
    return shaped


def estimate_results_chars(results: list[dict[str, Any]]) -> int:
    """Rough JSON size proxy for before/after budget checks (not token-accurate)."""
    return len(json.dumps(results, ensure_ascii=False, default=str))
