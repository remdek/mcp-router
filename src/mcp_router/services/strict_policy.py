"""Optional strict MCP surface: allow-list + read-only gate (treg /mcp vs /mcp/v2)."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Surface = Literal["default", "strict"]

_surface: ContextVar[Surface] = ContextVar("mcp_surface", default="default")


def set_surface(surface: Surface) -> None:
    _surface.set(surface)


def get_surface() -> Surface:
    return _surface.get()


def tool_key(server_name: str, tool_name: str) -> str:
    return f"{server_name}:{tool_name}"


def is_read_only_tool(tool_schema: dict[str, Any] | None) -> bool:
    """MCP ToolAnnotations.readOnlyHint; missing → not read-only."""
    if not isinstance(tool_schema, dict):
        return False
    annotations = tool_schema.get("annotations")
    if not isinstance(annotations, dict):
        return False
    hint = annotations.get("readOnlyHint")
    if hint is None:
        hint = annotations.get("read_only_hint")
    return hint is True


@dataclass(frozen=True)
class StrictPolicy:
    """Rules applied only on the strict surface (`/mcp/v2`)."""

    servers: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    read_only: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.servers) or bool(self.tools) or self.read_only

    def allows_server(self, server_name: str) -> bool:
        if not self.servers and not self.tools:
            return self.read_only
        if server_name in self.servers:
            return True
        return any(k.startswith(f"{server_name}:") for k in self.tools)

    def allows_tool(
        self,
        server_name: str,
        tool_name: str,
        *,
        tool_schema: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        key = tool_key(server_name, tool_name)
        if not self.servers and not self.tools:
            if not self.read_only:
                return (
                    False,
                    "strict profile has empty allow-list; set STRICT_ALLOWLIST or registry strictProfile",
                )
            allowlisted = True
        else:
            allowlisted = server_name in self.servers or key in self.tools
            if not allowlisted:
                return False, f"'{key}' is outside STRICT_ALLOWLIST"
        if self.read_only and not is_read_only_tool(tool_schema):
            return False, f"tool '{key}' is not read-only (strict read-only profile)"
        return True, ""

    def filter_tool_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in results:
            server = str(item.get("server_name", ""))
            tool = str(item.get("tool_name", ""))
            schema = item.get("tool_schema")
            if isinstance(schema, str):
                try:
                    schema = json.loads(schema)
                except json.JSONDecodeError:
                    schema = None
            ok, _ = self.allows_tool(
                server, tool, tool_schema=schema if isinstance(schema, dict) else None
            )
            if ok:
                out.append(item)
        return out

    def filter_server_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            r for r in results if self.allows_server(str(r.get("name", r.get("server_name", ""))))
        ]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "servers": sorted(self.servers),
            "tools": sorted(self.tools),
            "read_only": self.read_only,
            "configured": self.configured,
        }


def _split_tokens(items: list[str]) -> StrictPolicy:
    servers: set[str] = set()
    tools: set[str] = set()
    for token in items:
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            tools.add(token)
        else:
            servers.add(token)
    return StrictPolicy(servers=frozenset(servers), tools=frozenset(tools))


def _parse_allowlist_obj(data: Any, *, read_only_default: bool) -> StrictPolicy:
    if data is None:
        return StrictPolicy(read_only=read_only_default)
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return StrictPolicy(read_only=read_only_default)
        if text.startswith("@") or (text.endswith(".json") and Path(text).is_file()):
            path = Path(text[1:] if text.startswith("@") else text)
            return load_strict_policy_file(path, read_only_default=read_only_default)
        if text.startswith("{") or text.startswith("["):
            return _parse_allowlist_obj(json.loads(text), read_only_default=read_only_default)
        # CSV: server names and/or server:tool
        policy = _split_tokens(text.split(","))
        return StrictPolicy(
            servers=policy.servers,
            tools=policy.tools,
            read_only=read_only_default,
        )
    if isinstance(data, list):
        policy = _split_tokens([str(item) for item in data])
        return StrictPolicy(
            servers=policy.servers,
            tools=policy.tools,
            read_only=read_only_default,
        )
    if isinstance(data, dict):
        servers_raw = data.get("servers") or data.get("server") or []
        tools_raw = data.get("tools") or data.get("tool") or []
        if isinstance(servers_raw, str):
            servers_raw = [s.strip() for s in servers_raw.split(",") if s.strip()]
        if isinstance(tools_raw, str):
            tools_raw = [t.strip() for t in tools_raw.split(",") if t.strip()]
        read_only = data.get("read_only", data.get("readOnly", read_only_default))
        return StrictPolicy(
            servers=frozenset(str(s) for s in servers_raw),
            tools=frozenset(str(t) for t in tools_raw),
            read_only=bool(read_only),
        )
    raise ValueError(f"unsupported STRICT_ALLOWLIST shape: {type(data).__name__}")


def load_strict_policy_file(path: Path, *, read_only_default: bool = False) -> StrictPolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _parse_allowlist_obj(raw, read_only_default=read_only_default)


def build_strict_policy(
    *,
    allowlist: str = "",
    read_only: bool = False,
    registry_profile: dict[str, Any] | None = None,
) -> StrictPolicy:
    """Env STRICT_ALLOWLIST wins over registry strictProfile when set."""
    text = (allowlist or "").strip()
    if text:
        try:
            return _parse_allowlist_obj(text, read_only_default=read_only)
        except Exception as exc:
            logger.warning("Invalid STRICT_ALLOWLIST (%s); falling back to empty deny", exc)
            return StrictPolicy(read_only=read_only)
    if registry_profile:
        try:
            return _parse_allowlist_obj(registry_profile, read_only_default=read_only)
        except Exception as exc:
            logger.warning("Invalid registry strictProfile (%s); falling back to empty deny", exc)
            return StrictPolicy(read_only=read_only)
    return StrictPolicy(read_only=read_only)
