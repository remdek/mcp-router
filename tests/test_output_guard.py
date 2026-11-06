"""Tests for call_server_tool output guard (WOR-1538)."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_router.services.output_guard import (
    apply_output_guard,
    truncate_head,
    utf8_len,
)
from mcp_router.services.responses import remediation_error


def _payload(text: str, *, call_id: str = "call-1") -> dict:
    return {
        "ok": True,
        "call_id": call_id,
        "server_name": "srv",
        "tool_name": "tool",
        "result": {
            "content": [{"type": "text", "text": text}],
            "text": text,
            "is_error": False,
        },
        "error_source": None,
    }


def test_truncate_head_by_lines() -> None:
    text = "\n".join(f"line-{i}" for i in range(10))
    result = truncate_head(text, max_bytes=10_000, max_lines=3)
    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.output_lines == 3
    assert result.content == "line-0\nline-1\nline-2"


def test_truncate_head_by_bytes_utf8_safe() -> None:
    text = "ё" * 20  # 2 bytes each in UTF-8
    result = truncate_head(text, max_bytes=9, max_lines=100)
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert utf8_len(result.content) <= 9
    assert "ё" in result.content


def test_small_result_unchanged(tmp_path: Path) -> None:
    payload = _payload("hello")
    out = apply_output_guard(
        payload,
        max_bytes=50_000,
        max_lines=2000,
        artifacts_dir=tmp_path,
        call_id="c1",
    )
    assert out is payload or out == payload
    assert "output_guard" not in out
    assert list(tmp_path.iterdir()) == []


def test_huge_text_truncated_and_spilled(tmp_path: Path) -> None:
    huge = "x" * 5_000
    payload = _payload(huge, call_id="abc-123")
    out = apply_output_guard(
        payload,
        max_bytes=500,
        max_lines=2000,
        artifacts_dir=tmp_path,
        call_id="abc-123",
    )
    assert out["output_guard"]["truncated"] is True
    assert out["output_guard"]["truncated_by"] == "bytes"
    assert out["output_guard"]["artifact_id"] == "abc-123"
    path = Path(out["output_guard"]["full_output_path"])
    assert path.exists()
    assert path.parent == tmp_path
    spilled = json.loads(path.read_text(encoding="utf-8"))
    assert spilled["result"]["text"] == huge
    preview = out["result"]["text"]
    assert utf8_len(preview) < utf8_len(huge)
    assert "Full result saved to:" in preview
    assert out["result"]["is_error"] is False
    assert out["ok"] is True
    assert out["call_id"] == "abc-123"


def test_line_limit_triggers_spill(tmp_path: Path) -> None:
    text = "\n".join(f"L{i}" for i in range(50))
    out = apply_output_guard(
        _payload(text, call_id="lines"),
        max_bytes=100_000,
        max_lines=5,
        artifacts_dir=tmp_path,
        call_id="lines",
    )
    assert out["output_guard"]["truncated_by"] == "lines"
    assert out["output_guard"]["original_lines"] == 50


def test_disabled_passthrough(tmp_path: Path) -> None:
    huge = "y" * 10_000
    out = apply_output_guard(
        _payload(huge),
        enabled=False,
        max_bytes=100,
        max_lines=10,
        artifacts_dir=tmp_path,
        call_id="off",
    )
    assert "output_guard" not in out
    assert out["result"]["text"] == huge
    assert list(tmp_path.iterdir()) == []


def test_tool_error_shape_preserved_when_truncated(tmp_path: Path) -> None:
    text = "err\n" * 1000
    payload = _payload(text, call_id="err-1")
    payload["ok"] = False
    payload["result"]["is_error"] = True
    payload["error_source"] = "tool"
    out = apply_output_guard(
        payload,
        max_bytes=200,
        max_lines=20,
        artifacts_dir=tmp_path,
        call_id="err-1",
    )
    assert out["ok"] is False
    assert out["error_source"] == "tool"
    assert out["result"]["is_error"] is True
    assert out["output_guard"]["truncated"] is True


def test_remediation_error_not_guarded(tmp_path: Path) -> None:
    """failure_kind envelopes have no result — guard must leave them alone."""
    rem = remediation_error(
        "timeout",
        error="MCP stdio timeout (30s)",
        server_name="srv",
        tool_name="tool",
    )
    rem["call_id"] = "c-fail"
    out = apply_output_guard(
        rem,
        max_bytes=10,
        max_lines=1,
        artifacts_dir=tmp_path,
        call_id="c-fail",
    )
    assert out == rem
    assert out["failure_kind"] == "timeout"
    assert "output_guard" not in out
