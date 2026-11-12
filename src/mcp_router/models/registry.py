"""MCP registry configuration models."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GroupConfig(BaseModel):
    id: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)


class McpServerConfig(BaseModel):
    """Downstream MCP server entry from registry.json."""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    type: Literal["stdio", "http", "sse", "websocket"] | None = None
    description: str = ""
    groups: list[str] = Field(default_factory=list)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float | None = None
    working_directory: str | None = None
    skip_init: bool = False
    disabled: bool = False
    index: bool = True
    # Product lifecycle (pi-mcp-style). Prefer this over low-level session_mode.
    # lazy = reuse process, idle disconnect; keep-alive / lazy-keep-alive = stay
    # resident after first use (no idle shutdown). Unset preserves LAN defaults.
    lifecycle: Literal["lazy", "keep-alive", "lazy-keep-alive"] | None = None
    # Low-level stdio session: oneshot = spawn per request; persistent = reuse process.
    # Used when lifecycle is unset (legacy / browser allowlist defaults).
    session_mode: Literal["oneshot", "persistent"] | None = None
    idle_timeout_sec: float | None = None

    def to_transport_config(self) -> dict[str, Any]:
        # lifecycle is resolved by ServerManager before StdioTransport sees config.
        return self.model_dump(exclude_none=True, exclude={"lifecycle"})


class StrictProfileConfig(BaseModel):
    """Optional allow-list for the strict MCP surface (/mcp/v2)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    servers: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    read_only: bool = Field(default=False, alias="readOnly")


class InvokePolicyConfig(BaseModel):
    """Optional invoke-time deny/allow policy (applies on call, not search)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default: Literal["allow", "deny"] = "deny"
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    deny_destructive: bool = Field(default=False, alias="denyDestructive")


class Registry(BaseModel):
    """Root registry document (graphindex-compatible)."""

    model_config = ConfigDict(populate_by_name=True)

    groups: list[GroupConfig] = Field(default_factory=list)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
    strict_profile: StrictProfileConfig | None = Field(default=None, alias="strictProfile")
    invoke_policy: InvokePolicyConfig | None = Field(default=None, alias="invokePolicy")

    @model_validator(mode="before")
    @classmethod
    def inject_server_names(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        servers = data.get("mcpServers") or data.get("mcp_servers") or {}
        if isinstance(servers, dict):
            for server_id, cfg in servers.items():
                if isinstance(cfg, dict) and not cfg.get("name"):
                    cfg = dict(cfg)
                    cfg["name"] = server_id
                    servers[server_id] = cfg
        return data

    def to_manager_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)

    @classmethod
    def empty(cls) -> "Registry":
        return cls(mcp_servers={})
