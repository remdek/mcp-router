"""Server manager — connections to downstream MCP servers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from mcp_router.models.registry import GroupConfig, McpServerConfig, Registry
from mcp_router.models.tools import ServerInfo
from mcp_router.transports import (
    BaseTransport,
    HttpTransport,
    SseTransport,
    StdioTransport,
    WebSocketTransport,
)

logger = logging.getLogger(__name__)

_TOOLS_LIST_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

# Browser MCP needs one Chromium/session across tools/call (WOR-874).
_DEFAULT_PERSISTENT_STDIO = frozenset({"playwright", "chrome-devtools"})


class ServerManager:
    """Manages downstream MCP server transports."""

    def __init__(self, cache_service: Any | None = None) -> None:
        self._servers: dict[str, McpServerConfig] = {}
        self._transports: dict[str, BaseTransport] = {}
        self._cache_service = cache_service
        self._groups: list[GroupConfig] = []
        # StdioTransport may spawn a subprocess per request (oneshot) or keep one
        # alive (persistent). Serialize per server so health probes never race
        # an in-flight call (A3 / N3) and never tear a keep-alive session.
        self._stdio_locks: dict[str, asyncio.Lock] = {}

    def _stdio_lock(self, server_name: str) -> asyncio.Lock:
        lock = self._stdio_locks.get(server_name)
        if lock is None:
            lock = asyncio.Lock()
            self._stdio_locks[server_name] = lock
        return lock

    async def disconnect_server(self, server_name: str) -> None:
        transport = self._transports.pop(server_name, None)
        if transport:
            await transport.close()

    def load_registry(self, registry: Registry | dict[str, Any]) -> None:
        """Replace the in-memory server map (cold start / sync path)."""
        parsed = registry if isinstance(registry, Registry) else Registry.model_validate(registry)
        self._groups = parsed.groups
        servers: dict[str, McpServerConfig] = {}
        for name, config in parsed.mcp_servers.items():
            if config.disabled:
                logger.info("Skipping disabled server: %s", name)
                continue
            servers[name] = config
            logger.info("Registered server: %s", name)
        self._servers = servers

    async def apply_registry_update(self, registry: Registry | dict[str, Any]) -> list[str]:
        """Hot-reload: disconnect removed/changed servers, then replace configs.

        Returns server names that were added, removed, or reconfigured.
        """
        parsed = registry if isinstance(registry, Registry) else Registry.model_validate(registry)
        new_servers: dict[str, McpServerConfig] = {
            name: config for name, config in parsed.mcp_servers.items() if not config.disabled
        }
        changed: list[str] = []
        for name in list(self._servers):
            if name not in new_servers:
                await self.disconnect_server(name)
                self._stdio_locks.pop(name, None)
                changed.append(name)
                logger.info("Unregistered server: %s", name)
            elif self._servers[name] != new_servers[name]:
                await self.disconnect_server(name)
                changed.append(name)
                logger.info("Updated server: %s", name)
        for name in new_servers:
            if name not in self._servers:
                changed.append(name)
                logger.info("Registered server: %s", name)
        self._servers = new_servers
        self._groups = parsed.groups
        if self._cache_service is not None:
            self._cache_service.cleanup_all()
        return sorted(set(changed))

    def list_servers(self) -> list[ServerInfo]:
        return [
            ServerInfo(
                name=name,
                description=cfg.description,
                type=self._detect_transport_type(cfg),
            )
            for name, cfg in self._servers.items()
        ]

    def get_server_config(self, server_name: str) -> McpServerConfig | None:
        return self._servers.get(server_name)

    def get_groups(self) -> list[GroupConfig]:
        return self._groups

    def _detect_transport_type(self, config: McpServerConfig) -> str:
        explicit = (config.type or "").lower()
        if explicit in ("stdio", "http", "sse", "websocket"):
            return explicit
        url = config.url or ""
        if url.startswith(("ws://", "wss://")):
            return "websocket"
        if url.endswith("/sse"):
            return "sse"
        if url:
            return "http"
        return "stdio"

    def _resolve_stdio_session_mode(self, config: McpServerConfig) -> str:
        if config.session_mode:
            return config.session_mode
        if config.name in _DEFAULT_PERSISTENT_STDIO:
            return "persistent"
        return "oneshot"

    def _create_transport(self, config: McpServerConfig) -> BaseTransport:
        t = self._detect_transport_type(config)
        transport_config = config.to_transport_config()
        if t == "websocket":
            return WebSocketTransport(transport_config)
        if t == "sse":
            return SseTransport(transport_config)
        if t == "http":
            return HttpTransport(transport_config)
        if "session_mode" not in transport_config:
            transport_config["session_mode"] = self._resolve_stdio_session_mode(config)
        return StdioTransport(transport_config)

    def _get_transport(self, server_name: str) -> BaseTransport:
        if server_name not in self._servers:
            raise ValueError(f"Server not found: {server_name}")
        if server_name not in self._transports:
            self._transports[server_name] = self._create_transport(self._servers[server_name])
        return self._transports[server_name]

    async def get_server_tools(
        self, server_name: str, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        cache_key = f"tools:{server_name}"
        if use_cache and self._cache_service:
            cached = self._cache_service.get(cache_key)
            if cached is not None:
                return cast(list[dict[str, Any]], cached)

        config = self._servers.get(server_name)
        if config is not None and self._detect_transport_type(config) == "stdio":
            async with self._stdio_lock(server_name):
                tools = await self._tools_list_via_pool(server_name)
        else:
            tools = await self._tools_list_via_pool(server_name)

        if self._cache_service:
            self._cache_service.set(cache_key, tools)
        return tools

    async def _tools_list_via_pool(self, server_name: str) -> list[dict[str, Any]]:
        transport = self._get_transport(server_name)
        if not transport.is_connected:
            await transport.connect()
        result = await transport.send_request(_TOOLS_LIST_REQUEST)
        return cast(list[dict[str, Any]], result.get("tools", []))

    async def probe_server(self, server_name: str) -> None:
        """tools/list reachability check.

        Network transports use a one-shot connection (call-path pool untouched).
        Stdio reuses the pooled transport under a per-server lock; if a call is
        already in flight the probe is skipped (server is reachable) so a second
        subprocess is never spawned.
        """
        if server_name not in self._servers:
            raise ValueError(f"Server not found: {server_name}")
        config = self._servers[server_name]
        if self._detect_transport_type(config) == "stdio":
            await self._probe_stdio_server(server_name)
            return

        transport = self._create_transport(config)
        try:
            await transport.connect()
            await transport.send_request(_TOOLS_LIST_REQUEST)
        finally:
            await transport.close()

    async def _probe_stdio_server(self, server_name: str) -> None:
        lock = self._stdio_lock(server_name)
        if lock.locked():
            # Active call/list already proves the server is up; do not spawn.
            return
        async with lock:
            transport = self._transports.get(server_name)
            # Keep-alive browser session: live process ⇒ healthy; do not spawn
            # a second subprocess or tear the session with a cold oneshot.
            has_live = getattr(transport, "has_live_process", None)
            if (
                transport is not None
                and getattr(transport, "session_mode", None) == "persistent"
                and callable(has_live)
                and has_live()
            ):
                return
            await self._tools_list_via_pool(server_name)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_path: str | None = None,
    ) -> Any:
        config = self._servers.get(server_name)
        if config is not None and self._detect_transport_type(config) == "stdio":
            async with self._stdio_lock(server_name):
                return await self._call_tool_unlocked(
                    server_name, tool_name, arguments, workspace_path
                )
        return await self._call_tool_unlocked(server_name, tool_name, arguments, workspace_path)

    async def _call_tool_unlocked(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_path: str | None = None,
    ) -> Any:
        transport = self._get_transport(server_name)
        if not transport.is_connected:
            await transport.connect()

        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        kwargs: dict[str, Any] = {}
        if workspace_path and isinstance(transport, StdioTransport):
            kwargs["extra_env"] = {"WORKSPACE_FOLDER": workspace_path}

        result = await transport.send_request(request, **kwargs)
        return result.get("content", result)

    async def cleanup_all(self) -> None:
        for name in list(self._transports):
            try:
                await self._transports[name].close()
            except Exception as exc:
                logger.warning("Failed to close %s: %s", name, exc)
        self._transports.clear()
