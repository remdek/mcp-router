"""Tests for registry hot-reload (U7) and AppState wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_router.app_state import AppState, build_app_state
from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.models.settings import Settings
from mcp_router.services.cache_service import CacheService
from mcp_router.services.registry_hot_reload import RegistryHotReloader, registry_fingerprint
from mcp_router.services.server_manager import ServerManager


def test_build_app_state_holds_runtime_objects(tmp_path: Path) -> None:
    settings = Settings(
        registry_path=tmp_path / "registry.json",
        index_db_path=tmp_path / "tools.db",
        aliases_path=tmp_path / "aliases.json",
        reindex_on_start=False,
        health_probe_enabled=False,
        registry_hot_reload=False,
    )
    state = build_app_state(settings)
    assert isinstance(state, AppState)
    assert state.settings is settings
    assert state.server_manager is not None
    assert state.store is not None
    assert state.health_prober is not None
    assert state.strict_policy is not None


def test_registry_fingerprint_changes_on_write(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text('{"mcpServers":{}}', encoding="utf-8")
    first = registry_fingerprint(path)
    assert first is not None
    path.write_text(
        json.dumps({"mcpServers": {"a": {"url": "http://x/mcp"}}}),
        encoding="utf-8",
    )
    second = registry_fingerprint(path)
    assert second is not None
    assert second != first


async def test_hot_reloader_poll_once_invokes_callback(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text('{"mcpServers":{}}', encoding="utf-8")
    calls: list[int] = []

    async def on_change() -> None:
        calls.append(1)

    reloader = RegistryHotReloader(path, on_change, interval_sec=0.1, enabled=True)
    assert await reloader.poll_once() is False  # no change yet
    path.write_text(
        json.dumps({"mcpServers": {"srv": {"url": "http://example/mcp"}}}),
        encoding="utf-8",
    )
    assert await reloader.poll_once() is True
    assert calls == [1]
    assert await reloader.poll_once() is False


async def test_hot_reloader_disabled_skips(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")
    called = False

    async def on_change() -> None:
        nonlocal called
        called = True

    reloader = RegistryHotReloader(path, on_change, enabled=False)
    path.write_text('{"mcpServers":{}}', encoding="utf-8")
    assert await reloader.poll_once() is False
    assert called is False


async def test_apply_registry_update_removes_and_adds() -> None:
    cache = CacheService(ttl_seconds=60)
    cache.set("tools:old", [{"name": "x"}])
    manager = ServerManager(cache_service=cache)
    manager.load_registry(
        Registry(
            mcp_servers={
                "old": McpServerConfig(name="old", url="http://old/mcp"),
                "keep": McpServerConfig(name="keep", url="http://keep/mcp"),
            }
        )
    )
    # Simulate pooled transport for "old"
    transport = MagicMock()
    transport.close = AsyncMock()
    manager._transports["old"] = transport

    changed = await manager.apply_registry_update(
        Registry(
            mcp_servers={
                "keep": McpServerConfig(name="keep", url="http://keep/mcp"),
                "new": McpServerConfig(name="new", url="http://new/mcp"),
            }
        )
    )
    assert set(changed) == {"old", "new"}
    names = {s.name for s in manager.list_servers()}
    assert names == {"keep", "new"}
    transport.close.assert_awaited_once()
    assert cache.get("tools:old") is None


async def test_apply_registry_update_reconnects_on_config_change() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "srv": McpServerConfig(name="srv", url="http://v1/mcp"),
            }
        )
    )
    transport = MagicMock()
    transport.close = AsyncMock()
    manager._transports["srv"] = transport

    changed = await manager.apply_registry_update(
        Registry(
            mcp_servers={
                "srv": McpServerConfig(name="srv", url="http://v2/mcp"),
            }
        )
    )
    assert changed == ["srv"]
    transport.close.assert_awaited_once()
    assert "srv" not in manager._transports
    assert manager.get_server_config("srv") is not None
    assert manager.get_server_config("srv").url == "http://v2/mcp"


async def test_http_apply_registry_reload_schedules_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: file change → apply_registry_reload updates ServerManager."""
    from mcp_router import http_server as hs

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"mcpServers": {"a": {"url": "http://a/mcp"}}}),
        encoding="utf-8",
    )
    settings = Settings(
        registry_path=registry_path,
        index_db_path=tmp_path / "tools.db",
        aliases_path=tmp_path / "aliases.json",
        reindex_on_start=False,
        health_probe_enabled=False,
        registry_hot_reload=True,
        registry_poll_interval_sec=0.1,
    )
    new_state = build_app_state(settings)
    monkeypatch.setattr(hs, "state", new_state)

    scheduled: list[bool] = []
    monkeypatch.setattr(hs, "_schedule_reindex", lambda: scheduled.append(True))

    new_state.server_manager.load_registry(
        Registry(mcp_servers={"a": McpServerConfig(name="a", url="http://a/mcp")})
    )
    registry_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"url": "http://a/mcp"},
                    "b": {"url": "http://b/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    await hs._apply_registry_reload()
    names = {s.name for s in new_state.server_manager.list_servers()}
    assert names == {"a", "b"}
    assert scheduled == [True]
