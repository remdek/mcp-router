"""RU/EN aliases for servers and tools (SearchMiss → curated file)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AliasCatalog:
    """Load server/tool synonyms used in index text and search boost."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.servers: dict[str, list[str]] = {}
        self.tools: dict[str, list[str]] = {}
        if path is not None:
            self.reload()

    def reload(self) -> None:
        if self.path is None or not self.path.exists():
            self.servers = {}
            self.tools = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load aliases from %s: %s", self.path, exc)
            self.servers = {}
            self.tools = {}
            return
        self.servers = _normalize_map(raw.get("servers"))
        self.tools = _normalize_map(raw.get("tools"))

    def phrases_for(self, server_name: str, tool_name: str | None = None) -> list[str]:
        phrases: list[str] = []
        phrases.extend(self.servers.get(server_name, []))
        if tool_name:
            phrases.extend(self.tools.get(f"{server_name}:{tool_name}", []))
        return phrases

    def append_to_text(self, text: str, server_name: str, tool_name: str) -> str:
        extra = self.phrases_for(server_name, tool_name)
        if not extra:
            return text
        return f"{text} {' '.join(extra)}".strip()

    def matches(self, query: str) -> tuple[set[str], set[str]]:
        """Return (matched_servers, matched_tool_ids) for alias phrases in query."""
        q = query.lower()
        servers: set[str] = set()
        tools: set[str] = set()
        for tool_id, phrases in self.tools.items():
            if any(p.lower() in q for p in phrases if p):
                tools.add(tool_id)
        for server, phrases in self.servers.items():
            if any(p.lower() in q for p in phrases if p):
                servers.add(server)
        return servers, tools


def _normalize_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, phrases in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(phrases, str):
            cleaned = [phrases.strip()] if phrases.strip() else []
        elif isinstance(phrases, list):
            cleaned = [str(p).strip() for p in phrases if str(p).strip()]
        else:
            cleaned = []
        if cleaned:
            out[key] = cleaned
    return out


def load_aliases(path: Path) -> AliasCatalog:
    return AliasCatalog(path)
