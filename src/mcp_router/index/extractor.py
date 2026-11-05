"""Extract tool metadata from downstream MCP servers."""

import asyncio
import logging
from typing import Any

from mcp_router.models.tools import ToolMetadata
from mcp_router.services.aliases import AliasCatalog
from mcp_router.services.server_manager import ServerManager

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAY = 1.0


def _build_text_content(tool: dict[str, Any]) -> str:
    parts = [tool.get("description", ""), tool.get("name", "")]
    schema = tool.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for name, meta in props.items():
        if isinstance(meta, dict):
            parts.append(name)
            parts.append(meta.get("description", ""))
    return " ".join(p for p in parts if p)


def _enrich_text_content(
    server_name: str,
    tool: dict[str, Any],
    server_manager: ServerManager,
    aliases: AliasCatalog | None = None,
) -> str:
    parts = [_build_text_content(tool)]
    cfg = server_manager.get_server_config(server_name)
    if cfg and cfg.description:
        parts.append(cfg.description)
    group_map = {g.id: g for g in server_manager.get_groups()}
    if cfg:
        for group_id in cfg.groups:
            group = group_map.get(group_id)
            if not group:
                continue
            if group.description:
                parts.append(group.description)
            parts.extend(group.keywords)
    text = " ".join(p for p in parts if p)
    if aliases is not None:
        tool_name = tool.get("name", "")
        text = aliases.append_to_text(text, server_name, tool_name)
    return text


class ToolExtractor:
    def __init__(
        self,
        server_manager: ServerManager,
        timeout_sec: float = 60.0,
        concurrency: int = 4,
        aliases: AliasCatalog | None = None,
    ):
        self.server_manager = server_manager
        self.timeout_sec = timeout_sec
        self.concurrency = max(1, concurrency)
        self.aliases = aliases

    def _servers_to_index(self) -> list[str]:
        names: list[str] = []
        for server in self.server_manager.list_servers():
            cfg = self.server_manager.get_server_config(server.name)
            if cfg and not cfg.index:
                logger.info("Skipping index for server %s (index=false)", server.name)
                continue
            names.append(server.name)
        return names

    async def extract_all_tools(self) -> tuple[list[ToolMetadata], list[str]]:
        servers = self._servers_to_index()
        if not servers:
            return [], []

        semaphore = asyncio.Semaphore(self.concurrency)
        results: list[ToolMetadata] = []
        failed: list[str] = []

        async def extract_one(name: str) -> None:
            async with semaphore:
                try:
                    tools = await asyncio.wait_for(
                        self._extract_with_retry(name),
                        timeout=self._timeout_for(name),
                    )
                    results.extend(tools)
                except Exception as exc:
                    failed.append(name)
                    logger.warning("Failed to extract tools from %s: %s", name, exc)
                finally:
                    await self.server_manager.disconnect_server(name)

        await asyncio.gather(*(extract_one(name) for name in servers))
        return results, failed

    def _timeout_for(self, server_name: str) -> float:
        cfg = self.server_manager.get_server_config(server_name)
        if cfg and cfg.timeout:
            return float(cfg.timeout)
        return self.timeout_sec

    async def _extract_with_retry(self, server_name: str) -> list[ToolMetadata]:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                raw_tools = await self.server_manager.get_server_tools(server_name, use_cache=False)
                return [self._to_metadata(server_name, t) for t in raw_tools]
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise RuntimeError(f"extract failed for {server_name}: {last_exc}")

    def _to_metadata(self, server_name: str, tool: dict[str, Any]) -> ToolMetadata:
        tool_name = tool.get("name", "")
        description = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        params: list[dict[str, Any]] = []
        if isinstance(schema, dict):
            for pname, pmeta in schema.get("properties", {}).items():
                if isinstance(pmeta, dict):
                    params.append({"name": pname, **pmeta})
        text = _enrich_text_content(server_name, tool, self.server_manager, self.aliases)
        return ToolMetadata(
            tool_id=f"{server_name}:{tool_name}",
            server_name=server_name,
            tool_name=tool_name,
            description=description,
            text_content=text,
            parameters=params,
            tool_schema=tool,
        )
