"""Tests for registry loading."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_router.models.registry import Registry
from mcp_router.services.registry_loader import load_registry


def test_registry_injects_name_from_mcp_servers_key() -> None:
    registry = Registry.model_validate(
        {
            "mcpServers": {
                "browsermcp": {
                    "command": "npx",
                    "args": ["@browsermcp/mcp@latest"],
                    "description": "Browser automation",
                    "groups": ["research"],
                }
            }
        }
    )
    assert registry.mcp_servers["browsermcp"].name == "browsermcp"
    assert registry.mcp_servers["browsermcp"].command == "npx"


def test_registry_parses_lifecycle_and_idle_timeout() -> None:
    registry = Registry.model_validate(
        {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "lifecycle": "lazy",
                    "idle_timeout_sec": 300,
                },
                "browser": {
                    "command": "npx",
                    "lifecycle": "lazy-keep-alive",
                },
            }
        }
    )
    assert registry.mcp_servers["github"].lifecycle == "lazy"
    assert registry.mcp_servers["github"].idle_timeout_sec == 300
    assert registry.mcp_servers["browser"].lifecycle == "lazy-keep-alive"
    dumped = registry.mcp_servers["github"].to_transport_config()
    assert "lifecycle" not in dumped
    assert dumped["idle_timeout_sec"] == 300


def test_load_registry_from_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "context7": {
                        "url": "https://mcp.context7.com/mcp",
                        "headers": {"Authorization": "Bearer test"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = load_registry(path)
    assert registry.mcp_servers["context7"].name == "context7"
