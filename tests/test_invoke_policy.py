"""Tests for invoke-time deny/allow/denyDestructive policy (WOR-1612)."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_router.services.invoke_policy import (
    InvokePolicy,
    build_invoke_policy,
    is_destructive_tool,
    match_glob,
)
from mcp_router.services.responses import remediation_error


def _schema(*, destructive: bool | None = None) -> dict | None:
    if destructive is None:
        return {}
    return {"annotations": {"destructiveHint": destructive}}


def test_inactive_policy_allows_everything() -> None:
    policy = InvokePolicy(active=False)
    assert policy.check("playwright", "browser_click").allowed is True
    assert "inactive" in policy.check("x", "y").rule


def test_deny_wins_over_allow_and_default() -> None:
    policy = InvokePolicy(
        active=True,
        default="allow",
        allow=("deploy:*",),
        deny=("deploy:rollback",),
        deny_destructive=True,
    )
    assert (
        policy.check("deploy", "rollback", tool_schema=_schema(destructive=False)).allowed is False
    )
    assert 'deny "deploy:rollback"' in policy.check("deploy", "rollback").rule


def test_allow_overrides_deny_destructive() -> None:
    policy = InvokePolicy(
        active=True,
        default="allow",
        allow=("deploy:*",),
        deny=(),
        deny_destructive=True,
    )
    assert policy.check("deploy", "restart", tool_schema=_schema(destructive=True)).allowed is True
    assert (
        policy.check("crm", "delete_lead", tool_schema=_schema(destructive=True)).allowed is False
    )
    assert policy.check("crm", "get_lead", tool_schema=_schema(destructive=False)).allowed is True


def test_missing_destructive_annotation_not_treated_as_destructive() -> None:
    policy = InvokePolicy(active=True, default="allow", deny_destructive=True)
    assert policy.check("any", "write", tool_schema={}).allowed is True
    assert policy.check("any", "write", tool_schema=None).allowed is True
    assert is_destructive_tool({}) is False
    assert is_destructive_tool({"annotations": {"destructiveHint": True}}) is True


def test_default_deny_without_allow() -> None:
    policy = InvokePolicy(active=True, default="deny", allow=("hindsight:recall",))
    assert policy.check("hindsight", "recall").allowed is True
    assert policy.check("hindsight", "retain").allowed is False
    assert policy.check("hindsight", "retain").rule == "default deny"


def test_slash_and_colon_glob_forms() -> None:
    policy = InvokePolicy(active=True, default="deny", allow=("hindsight/*",))
    assert policy.check("hindsight", "recall").allowed is True
    policy2 = InvokePolicy(active=True, default="deny", deny=("playwright:*",))
    assert policy2.check("playwright", "browser_click").allowed is False


def test_glob_cases() -> None:
    assert match_glob("deploy:*", "deploy:rollback")
    assert match_glob("*/delete_*", "crm/delete_lead")
    assert match_glob("a*b*c", "aXbYc")
    assert not match_glob("a*b*c", "aXcYb")
    assert not match_glob("deploy:*", "vcs:search")
    assert match_glob("exact:name", "exact:name")
    assert not match_glob("ab*ba", "aba")
    assert match_glob("*", "")


def test_build_inactive_when_empty() -> None:
    assert build_invoke_policy(policy="").active is False
    assert build_invoke_policy(policy="", registry_policy=None).active is False


def test_build_from_json_env_and_default_deny_when_omitted() -> None:
    policy = build_invoke_policy(policy='{"allow":["hindsight:*"],"denyDestructive":true}')
    assert policy.active is True
    assert policy.default == "deny"
    assert policy.deny_destructive is True
    assert policy.check("hindsight", "recall").allowed is True
    assert policy.check("other", "x").allowed is False


def test_build_from_registry_when_env_empty() -> None:
    policy = build_invoke_policy(
        policy="",
        registry_policy={"default": "allow", "deny": ["playwright:*"]},
    )
    assert policy.active is True
    assert policy.check("playwright", "click").allowed is False
    assert policy.check("docs", "lookup").allowed is True


def test_env_overrides_registry() -> None:
    policy = build_invoke_policy(
        policy='{"default":"deny","allow":["hindsight:*"]}',
        registry_policy={"default": "allow", "deny": ["hindsight:*"]},
    )
    assert policy.check("hindsight", "recall").allowed is True


def test_unknown_keys_fall_back_inactive_from_env() -> None:
    policy = build_invoke_policy(policy='{"default":"allow","denylist":["a:*"]}')
    assert policy.active is False


def test_build_from_file(tmp_path: Path) -> None:
    path = tmp_path / "invoke.json"
    path.write_text(
        json.dumps({"default": "deny", "allow": ["searxng:*"]}),
        encoding="utf-8",
    )
    policy = build_invoke_policy(policy=str(path))
    assert policy.check("searxng", "searxng_web_search").allowed is True
    assert policy.check("github", "create_issue").allowed is False


def test_policy_denied_remediation_shape() -> None:
    payload = remediation_error(
        "policy_denied",
        error='deny "playwright:*"',
        server_name="playwright",
        tool_name="browser_click",
        extra={"permission": {"decision": "deny", "rule": 'deny "playwright:*"'}},
    )
    assert payload["ok"] is False
    assert payload["failure_kind"] == "policy_denied"
    assert payload["error_source"] == "router"
    assert payload["permission"]["decision"] == "deny"
    assert "search_tools" in payload["reconnect_hint"] or "permission" in payload["reconnect_hint"]


def test_search_does_not_import_invoke_filter_contract() -> None:
    """Documented: invoke policy must not filter search_tools results."""
    from mcp_router.services import invoke_policy as mod

    assert not hasattr(mod.InvokePolicy, "filter_tool_results")
