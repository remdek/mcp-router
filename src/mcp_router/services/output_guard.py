"""Output guard for call_server_tool (WOR-1538 / ADR 0001 Layer C).

Caps agent-facing call results (bytes + lines). Oversized payloads spill the
full JSON to ``data/call_artifacts/`` and return a head preview + pointer.
Pattern: pi-mcp-adapter ``mcp-output-guard`` (50 KiB / 2000 lines).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_MAX_BYTES = 50 * 1024  # 50 KiB — matches Pi bash / MCP output guard
DEFAULT_MAX_LINES = 2000

TruncatedBy = Literal["bytes", "lines", "none"]


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: TruncatedBy
    total_bytes: int
    total_lines: int
    output_bytes: int
    output_lines: int
    max_bytes: int
    max_lines: int


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def truncate_head(text: str, *, max_bytes: int, max_lines: int) -> TruncationResult:
    """Keep a UTF-8-safe head of ``text`` within both byte and line caps."""
    total_bytes = utf8_len(text)
    total_lines = line_count(text)
    safe_bytes = max(0, int(max_bytes))
    safe_lines = max(0, int(max_lines))

    by_lines = text
    truncated_by: TruncatedBy = "none"
    if safe_lines > 0 and total_lines > safe_lines:
        parts = text.split("\n", safe_lines)
        by_lines = "\n".join(parts[:safe_lines])
        truncated_by = "lines"
    elif safe_lines == 0 and total_lines > 0:
        by_lines = ""
        truncated_by = "lines"

    raw = by_lines.encode("utf-8")
    if safe_bytes == 0 and total_bytes > 0:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_bytes=total_bytes,
            total_lines=total_lines,
            output_bytes=0,
            output_lines=0,
            max_bytes=safe_bytes,
            max_lines=safe_lines,
        )
    if len(raw) > safe_bytes:
        cut = raw[:safe_bytes]
        # Avoid splitting a multi-byte UTF-8 sequence.
        while cut and (cut[-1] & 0xC0) == 0x80:
            cut = cut[:-1]
        by_lines = cut.decode("utf-8", errors="ignore")
        truncated_by = "bytes"

    out_bytes = utf8_len(by_lines)
    out_lines = line_count(by_lines)
    truncated = truncated_by != "none" or out_bytes < total_bytes or out_lines < total_lines
    if not truncated:
        truncated_by = "none"
    return TruncationResult(
        content=by_lines,
        truncated=truncated,
        truncated_by=truncated_by if truncated else "none",
        total_bytes=total_bytes,
        total_lines=total_lines,
        output_bytes=out_bytes,
        output_lines=out_lines,
        max_bytes=safe_bytes,
        max_lines=safe_lines,
    )


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return f"{num_bytes / (1024 * 1024):.1f} MiB"


def _notice(
    truncation: TruncationResult,
    *,
    full_output_path: str | None,
    artifact_id: str | None,
    write_error: str | None,
) -> str:
    if truncation.truncated_by == "lines":
        reason = (
            f"showing {truncation.output_lines} of {truncation.total_lines} lines "
            f"({truncation.max_lines} line limit)"
        )
    else:
        reason = (
            f"{truncation.output_lines} lines shown ({format_size(truncation.max_bytes)} limit)"
        )
    base = (
        f"[mcp-router output truncated: original {truncation.total_lines} lines / "
        f"{format_size(truncation.total_bytes)}. Truncated: {reason}."
    )
    if full_output_path:
        aid = f" artifact_id={artifact_id}" if artifact_id else ""
        return (
            f"{base} Full result saved to: {full_output_path}{aid} — "
            f"read with offset/limit or grep to inspect.]"
        )
    return f"{base} Full output could not be saved: {write_error or 'unknown error'}]"


def _payload_text(payload: dict[str, Any]) -> str:
    """Agent-facing text used for size checks (prefer result.text, else JSON)."""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text:
            return text
        content = result.get("content")
        if isinstance(content, str) and content:
            return content
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except TypeError:
            return str(result)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        return str(payload)


def save_call_artifact(artifacts_dir: Path, artifact_id: str, body: str) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    # Keep filename filesystem-safe (call_id is usually a UUID).
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", artifact_id)[:120] or "artifact"
    path = artifacts_dir / f"{safe_id}.json"
    path.write_text(body, encoding="utf-8")
    return path


def apply_output_guard(
    payload: dict[str, Any],
    *,
    enabled: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
    artifacts_dir: Path,
    call_id: str,
) -> dict[str, Any]:
    """Return payload unchanged or with truncated ``result`` + ``output_guard``.

    Does not alter remediation / ``failure_kind`` envelopes (caller should only
    pass successful or tool-error call payloads that already have ``result``).
    """
    if not enabled or "result" not in payload:
        return payload

    full_json = json.dumps(payload, ensure_ascii=False, default=str)
    text = _payload_text(payload)
    # Compact JSON is often one line — check text (lines) and envelope (bytes).
    text_probe = truncate_head(text, max_bytes=max_bytes, max_lines=max_lines)
    json_probe = truncate_head(full_json, max_bytes=max_bytes, max_lines=max_lines)
    if not text_probe.truncated and not json_probe.truncated:
        return payload
    # Prefer human-readable text for the inline preview when it was oversized;
    # otherwise spill/preview from the full envelope JSON.
    if text_probe.truncated:
        measure = text
        probe = text_probe
    else:
        measure = full_json
        probe = json_probe

    artifact_id = call_id
    full_path: str | None = None
    write_error: str | None = None
    try:
        path = save_call_artifact(artifacts_dir, artifact_id, full_json)
        full_path = str(path)
    except OSError as exc:
        write_error = str(exc)

    notice = _notice(
        probe,
        full_output_path=full_path,
        artifact_id=artifact_id,
        write_error=write_error,
    )
    # Reserve budget for the notice so the final string stays near the caps.
    notice_bytes = utf8_len(f"\n\n{notice}")
    notice_lines = line_count(f"\n\n{notice}")
    preview = truncate_head(
        measure,
        max_bytes=max(0, max_bytes - notice_bytes),
        max_lines=max(0, max_lines - notice_lines),
    )
    final_text = f"{preview.content}\n\n{notice}" if preview.content else notice
    final_stats = truncate_head(final_text, max_bytes=10**9, max_lines=10**9)

    result = dict(payload.get("result") or {})
    is_error = bool(result.get("is_error"))
    guarded_result = {
        "content": [{"type": "text", "text": final_text}],
        "text": final_text,
        "is_error": is_error,
    }

    out = dict(payload)
    out["result"] = guarded_result
    out["output_guard"] = {
        "truncated": True,
        "original_bytes": probe.total_bytes,
        "original_lines": probe.total_lines,
        "returned_bytes": final_stats.total_bytes,
        "returned_lines": final_stats.total_lines,
        "truncated_by": probe.truncated_by,
        "max_bytes": max_bytes,
        "max_lines": max_lines,
        "artifact_id": artifact_id,
        **({"full_output_path": full_path} if full_path else {}),
        **({"write_error": write_error} if write_error else {}),
    }
    return out


def default_artifacts_dir(index_db_path: Path) -> Path:
    """Spill next to the SQLite index (``data/call_artifacts`` by default)."""
    return Path(index_db_path).expanduser().resolve().parent / "call_artifacts"
