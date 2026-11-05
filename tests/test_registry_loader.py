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
