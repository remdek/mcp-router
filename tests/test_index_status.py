"""Tests for IndexStatus parsing."""

from mcp_router.models.tools import IndexStatus


def test_index_status_from_rebuild_result() -> None:
    result = {
        "status": "ready",
        "ok": True,
        "tool_count": 2,
        "embedding_ok": False,
        "last_indexed_at": "2026-01-01T00:00:00+00:00",
        "mode": "keyword_only",
    }
    status = IndexStatus.model_validate(result)
    assert status.status == "ready"
    assert status.tool_count == 2
    assert status.embedding_ok is False
