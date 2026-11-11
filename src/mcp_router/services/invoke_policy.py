"""Invoke-time permission policy (ToolLens-style). Discovery never grants permission.

Checked on ``call_server_tool`` (and reflected on ``get_tool_schema``). ``search_tools``
does not consult this policy. Independent of the strict ``/mcp/v2`` allow-list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

DefaultDecision = Literal["allow", "deny"]
Decision = Literal["allow", "deny"]


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule: str

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def to_public_dict(self) -> dict[str, str]:
        return {"decision": self.decision, "rule": self.rule}


def tool_key(server_name: str, tool_name: str) -> str:
    """Canonical tool id for globs (``server:tool``)."""
    return f"{server_name}:{tool_name}"


def is_destructive_tool(tool_schema: dict[str, Any] | None) -> bool:
    """MCP ToolAnnotations.destructiveHint; missing → not destructive (documented caveat)."""
    if not isinstance(tool_schema, dict):
        return False
    annotations = tool_schema.get("annotations")
    if not isinstance(annotations, dict):
        return False
    hint = annotations.get("destructiveHint")
    if hint is None:
        hint = annotations.get("destructive_hint")
    return hint is True


def match_glob(pattern: str, value: str) -> bool:
    """``*``-only glob matched against the whole string (ToolLens semantics)."""
    parts = pattern.split("*")
    if len(parts) == 1:
        return pattern == value
    first, last = parts[0], parts[-1]
    if not value.startswith(first):
        return False
    if len(value) < len(first) + len(last):
        return False
    if not value.endswith(last):
        return False
    rest = value[len(first) : len(value) - len(last)]
    for mid in parts[1:-1]:
        idx = rest.find(mid)
        if idx < 0:
            return False
        rest = rest[idx + len(mid) :]
    return True


def _candidate_ids(server_name: str, tool_name: str) -> tuple[str, str]:
    """Native ``server:tool`` plus ToolLens-style ``server/tool``."""
    return (f"{server_name}:{tool_name}", f"{server_name}/{tool_name}")


def _glob_matches(pattern: str, server_name: str, tool_name: str) -> bool:
    return any(
        match_glob(pattern, candidate) for candidate in _candidate_ids(server_name, tool_name)
    )


@dataclass(frozen=True)
class InvokePolicy:
    """Permission rules for invoke. Inactive policy preserves open ``/mcp`` behavior."""

    default: DefaultDecision = "allow"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    deny_destructive: bool = False
    active: bool = False

    def check(
        self,
        server_name: str,
        tool_name: str,
        *,
        tool_schema: dict[str, Any] | None = None,
    ) -> Verdict:
        if not self.active:
            return Verdict("allow", "invoke policy inactive (open)")

        for pattern in self.deny:
            if _glob_matches(pattern, server_name, tool_name):
                return Verdict("deny", f'deny "{pattern}"')

        allowed_by = next(
            (p for p in self.allow if _glob_matches(p, server_name, tool_name)),
            None,
        )
        if self.deny_destructive and is_destructive_tool(tool_schema) and allowed_by is None:
            return Verdict(
                "deny",
                "denyDestructive (server marks this tool destructive)",
            )
        if allowed_by is not None:
            return Verdict("allow", f'allow "{allowed_by}"')
        if self.default == "allow":
            return Verdict("allow", "default allow")
        return Verdict("deny", "default deny")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "default": self.default,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "deny_destructive": self.deny_destructive,
        }


_INACTIVE = InvokePolicy(active=False)


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValueError(f"expected string or list of globs, got {type(raw).__name__}")


def _parse_policy_obj(data: Any) -> InvokePolicy:
    if data is None:
        return _INACTIVE
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return _INACTIVE
        if text.startswith("@") or (text.endswith(".json") and Path(text).is_file()):
            path = Path(text[1:] if text.startswith("@") else text)
            return load_invoke_policy_file(path)
        if text.startswith("{") or text.startswith("["):
            return _parse_policy_obj(json.loads(text))
        raise ValueError("INVOKE_POLICY string must be JSON object, @path, or .json path")
    if isinstance(data, list):
        raise ValueError("INVOKE_POLICY must be an object, not a bare list")
    if not isinstance(data, dict):
        raise ValueError(f"unsupported INVOKE_POLICY shape: {type(data).__name__}")

    unknown = set(data) - {
        "default",
        "allow",
        "deny",
        "denyDestructive",
        "deny_destructive",
    }
    if unknown:
        raise ValueError(f"unknown invoke policy keys: {sorted(unknown)}")

    default_raw = data.get("default", "deny")
    if default_raw not in ("allow", "deny"):
        raise ValueError("invoke policy default must be 'allow' or 'deny'")
    deny_destructive = bool(data.get("denyDestructive", data.get("deny_destructive", False)))
    return InvokePolicy(
        default=default_raw,  # type: ignore[arg-type]
        allow=tuple(_as_str_list(data.get("allow"))),
        deny=tuple(_as_str_list(data.get("deny"))),
        deny_destructive=deny_destructive,
        active=True,
    )


def load_invoke_policy_file(path: Path) -> InvokePolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _parse_policy_obj(raw)


def build_invoke_policy(
    *,
    policy: str = "",
    registry_policy: dict[str, Any] | None = None,
) -> InvokePolicy:
    """Env ``INVOKE_POLICY`` wins over registry ``invokePolicy`` when set."""
    text = (policy or "").strip()
    if text:
        try:
            return _parse_policy_obj(text)
        except Exception as exc:
            logger.warning("Invalid INVOKE_POLICY (%s); falling back to inactive (open)", exc)
            return _INACTIVE
    if registry_policy:
        try:
            return _parse_policy_obj(registry_policy)
        except Exception as exc:
            logger.warning(
                "Invalid registry invokePolicy (%s); falling back to inactive (open)", exc
            )
            return _INACTIVE
    return _INACTIVE
