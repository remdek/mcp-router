"""Tests for response normalization and typed remediation errors."""

from mcp_router.services.responses import (
    assess_search_confidence,
    classify_call_error,
    classify_failure_kind,
    group_results_by_server,
    normalize_tool_result,
    remediation_error,
    sanitize_error_message,
)


def test_group_results_by_server() -> None:
    results = [
        {"server_name": "a", "tool_name": "t1", "score": 0.1},
        {"server_name": "a", "tool_name": "t2", "score": 0.09},
        {"server_name": "a", "tool_name": "t3", "score": 0.08},
        {"server_name": "b", "tool_name": "t1", "score": 0.07},
    ]
    grouped = group_results_by_server(results, max_per_server=2)
    assert len(grouped) == 3
    assert sum(1 for r in grouped if r["server_name"] == "a") == 2


def test_assess_search_confidence_high() -> None:
    confidence, _ = assess_search_confidence(
        [{"score": 0.05}, {"score": 0.02}],
    )
    assert confidence == "high"


def test_assess_search_confidence_none() -> None:
    confidence, hint = assess_search_confidence([])
    assert confidence == "none"
    assert "search_servers" in hint


def test_normalize_tool_result_mcp_content() -> None:
    normalized = normalize_tool_result(
        {"content": [{"type": "text", "text": "hello"}], "isError": False}
    )
    assert normalized["text"] == "hello"
    assert normalized["is_error"] is False


def test_classify_call_error_router() -> None:
    source, _ = classify_call_error(ValueError("Server not found: x"), "x")
    assert source == "router"


def test_classify_call_error_downstream() -> None:
    source, _ = classify_call_error(RuntimeError("MCP stdio timeout (30s)"), "x")
    assert source == "downstream"


def test_classify_failure_kind_timeout() -> None:
    assert classify_failure_kind(RuntimeError("MCP stdio timeout (30s)")) == "timeout"


def test_classify_failure_kind_auth() -> None:
    assert (
        classify_failure_kind(RuntimeError("Unauthorized for http://x: check API key headers"))
        == "auth_failed"
    )


def test_classify_failure_kind_unreachable() -> None:
    assert (
        classify_failure_kind(RuntimeError("Connection refused to localhost:9009"))
        == "downstream_unreachable"
    )


def test_classify_failure_kind_tool_not_found() -> None:
    assert classify_failure_kind(ValueError("Server not found: foo")) == "tool_not_found"


def test_remediation_error_payload_shape() -> None:
    payload = remediation_error(
        "timeout",
        error="MCP stdio timeout (30s)",
        server_name="hindsight",
        tool_name="recall",
    )
    assert payload["ok"] is False
    assert payload["failure_kind"] == "timeout"
    assert payload["server_name"] == "hindsight"
    assert payload["tool_name"] == "recall"
    assert payload["error_source"] == "downstream"
    assert "retry" in payload["next_action"].lower() or "Retry" in payload["next_action"]
    assert payload["reconnect_hint"]
    assert payload["retry_hint"] == payload["next_action"]


def test_sanitize_error_message_redacts_bearer() -> None:
    raw = "Unauthorized: Authorization: Bearer super-secret-token-value"
    cleaned = sanitize_error_message(raw)
    assert "super-secret-token-value" not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_api_key() -> None:
    raw = "failed api_key=sk-live-abc123 header dump"
    cleaned = sanitize_error_message(raw)
    assert "sk-live-abc123" not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_openai_sk() -> None:
    raw = "upstream rejected key sk-proj-AbCdEfGhIjKlMnOpQrSt"
    cleaned = sanitize_error_message(raw)
    assert "sk-proj-AbCdEfGhIjKlMnOpQrSt" not in cleaned
    assert "sk-[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_github_pat() -> None:
    raw = "git auth failed ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    cleaned = sanitize_error_message(raw)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in cleaned
    assert "ghp_[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_aws_akia() -> None:
    raw = "AWS AccessKeyId=AKIAIOSFODNN7EXAMPLE was denied"
    cleaned = sanitize_error_message(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert "AKIA[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    raw = f"token leak: {jwt}"
    cleaned = sanitize_error_message(raw)
    assert jwt not in cleaned
    assert "eyJhbGci" not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_slack_token() -> None:
    raw = "slack bot xoxb-1234567890-abcdefghij failed"
    cleaned = sanitize_error_message(raw)
    assert "xoxb-1234567890-abcdefghij" not in cleaned
    assert "xoxb-[REDACTED]" in cleaned


def test_sanitize_error_message_redacts_gitlab_pat() -> None:
    raw = "gitlab glpat-abcdefghijklmnopqrstuvwx rejected"
    cleaned = sanitize_error_message(raw)
    assert "glpat-abcdefghijklmnopqrstuvwx" not in cleaned
    assert "glpat-[REDACTED]" in cleaned


def test_remediation_error_sanitizes_message() -> None:
    payload = remediation_error(
        "auth_failed",
        error="Unauthorized Bearer abc.def.ghi",
        server_name="graphiti",
    )
    assert "abc.def.ghi" not in payload["error"]
    assert payload["failure_kind"] == "auth_failed"
