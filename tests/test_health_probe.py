"""Tests for periodic downstream health probes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from mcp_router.models.registry import McpServerConfig, Registry
from mcp_router.services.health_probe import DownstreamHealthProber
from mcp_router.services.server_manager import ServerManager


@pytest.fixture
def two_server_manager() -> ServerManager:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "ok": McpServerConfig(name="ok", type="http", url="http://mock/ok"),
                "broken": McpServerConfig(name="broken", type="http", url="http://mock/broken"),
            }
        )
    )
    return manager


async def test_probe_classifies_healthy_and_failed(two_server_manager: ServerManager) -> None:
    async def probe(name: str) -> None:
        if name == "broken":
            raise RuntimeError("connection refused")

    two_server_manager.probe_server = AsyncMock(side_effect=probe)  # type: ignore[method-assign]
    prober = DownstreamHealthProber(
        two_server_manager,
        interval_sec=60.0,
        timeout_sec=1.0,
        concurrency=2,
        enabled=True,
    )

    state = await prober.probe_once()
    snap = prober.snapshot()

    assert state.last_probe_at is not None
    assert snap["probe_in_progress"] is False
    assert snap["healthy_servers"] == ["ok"]
    assert snap["failed_servers"] == ["broken"]
    assert snap["servers"]["ok"]["healthy"] is True
    assert snap["servers"]["ok"]["error"] is None
    assert snap["servers"]["broken"]["healthy"] is False
    assert "connection refused" in (snap["servers"]["broken"]["error"] or "")


async def test_probe_timeout_marks_failed(two_server_manager: ServerManager) -> None:
    async def slow(name: str) -> None:
        if name == "ok":
            return
        await asyncio.sleep(2.0)

    two_server_manager.probe_server = AsyncMock(side_effect=slow)  # type: ignore[method-assign]
    prober = DownstreamHealthProber(
        two_server_manager,
        timeout_sec=0.05,
        concurrency=2,
    )

    await prober.probe_once()
    snap = prober.snapshot()

    assert snap["healthy_servers"] == ["ok"]
    assert snap["failed_servers"] == ["broken"]
    assert snap["servers"]["broken"]["healthy"] is False


async def test_snapshot_is_non_blocking_during_probe(two_server_manager: ServerManager) -> None:
    release = asyncio.Event()

    async def hang(_name: str) -> None:
        await release.wait()

    two_server_manager.probe_server = AsyncMock(side_effect=hang)  # type: ignore[method-assign]
    prober = DownstreamHealthProber(two_server_manager, timeout_sec=5.0, concurrency=2)

    task = asyncio.create_task(prober.probe_once())
    await asyncio.sleep(0.05)
    snap = prober.snapshot()
    assert snap["probe_in_progress"] is True
    assert snap["failed_servers"] == []
    assert snap["healthy_servers"] == []

    release.set()
    await task
    final = prober.snapshot()
    assert final["probe_in_progress"] is False
    assert set(final["healthy_servers"]) == {"broken", "ok"}


async def test_probe_server_uses_oneshot_transport_not_pool() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "tasks": McpServerConfig(name="tasks", type="http", url="http://mock/tasks"),
            }
        )
    )
    created: list[object] = []

    class FakeTransport:
        def __init__(self) -> None:
            self.closed = False
            self.is_connected = False

        async def connect(self) -> None:
            self.is_connected = True

        async def send_request(self, request: dict, **kwargs: object) -> dict:
            assert request["method"] == "tools/list"
            return {"tools": []}

        async def close(self) -> None:
            self.closed = True
            self.is_connected = False

    def create(_cfg: object) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    manager._create_transport = create  # type: ignore[method-assign]

    await manager.probe_server("tasks")

    assert len(created) == 1
    assert created[0].closed is True  # type: ignore[attr-defined]
    assert manager._transports == {}


async def test_stdio_probe_reuses_pool_not_oneshot() -> None:
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "local": McpServerConfig(name="local", command="python3", args=["-c", "pass"]),
            }
        )
    )
    created: list[object] = []

    class FakeTransport:
        def __init__(self) -> None:
            self.is_connected = False

        async def connect(self) -> None:
            self.is_connected = True

        async def send_request(self, request: dict, **kwargs: object) -> dict:
            assert request["method"] == "tools/list"
            return {"tools": []}

        async def close(self) -> None:
            self.is_connected = False

    def create(_cfg: object) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    manager._create_transport = create  # type: ignore[method-assign]

    await manager.probe_server("local")

    assert len(created) == 1
    assert "local" in manager._transports
    assert manager._transports["local"] is created[0]


async def test_stdio_probe_skips_second_subprocess_during_active_call() -> None:
    """A3: while call holds the stdio lock, probe must not spawn another process."""
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "local": McpServerConfig(name="local", command="python3", args=["-c", "pass"]),
            }
        )
    )

    call_entered = asyncio.Event()
    release_call = asyncio.Event()
    spawn_count = 0
    peak_concurrent = 0
    concurrent = 0

    class FakeTransport:
        def __init__(self) -> None:
            self.is_connected = True

        async def connect(self) -> None:
            self.is_connected = True

        async def send_request(self, request: dict, **kwargs: object) -> dict:
            nonlocal spawn_count, peak_concurrent, concurrent
            # Each send_request models one subprocess lifetime.
            spawn_count += 1
            concurrent += 1
            peak_concurrent = max(peak_concurrent, concurrent)
            try:
                if request.get("method") == "tools/call":
                    call_entered.set()
                    await release_call.wait()
                    return {"content": [{"type": "text", "text": "ok"}]}
                return {"tools": []}
            finally:
                concurrent -= 1

        async def close(self) -> None:
            self.is_connected = False

    manager._create_transport = lambda _cfg: FakeTransport()  # type: ignore[method-assign]

    call_task = asyncio.create_task(manager.call_tool("local", "echo", {"text": "hi"}))
    await call_entered.wait()
    assert concurrent == 1

    await manager.probe_server("local")

    assert concurrent == 1
    assert spawn_count == 1
    assert peak_concurrent == 1

    release_call.set()
    await call_task
    assert peak_concurrent == 1


async def test_stdio_probe_skips_when_stdio_lock_held() -> None:
    """A3: lock held ⇒ probe returns without creating a transport/subprocess."""
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "local": McpServerConfig(name="local", command="python3", args=["-c", "pass"]),
            }
        )
    )
    creates = 0

    def create(_cfg: object) -> object:
        nonlocal creates
        creates += 1
        raise AssertionError("probe must not create a transport while call lock is held")

    manager._create_transport = create  # type: ignore[method-assign]
    lock = manager._stdio_lock("local")
    await lock.acquire()
    try:
        await manager.probe_server("local")
        assert creates == 0
        assert manager._transports == {}
    finally:
        lock.release()


async def test_persistent_stdio_probe_does_not_tear_keepalive() -> None:
    """WOR-874: probe with a live persistent process must not spawn or tools/list."""
    manager = ServerManager()
    manager.load_registry(
        Registry(
            mcp_servers={
                "playwright": McpServerConfig(
                    name="playwright", command="python3", args=["-c", "pass"]
                ),
            }
        )
    )
    send_calls = 0

    class FakePersistent:
        session_mode = "persistent"

        def __init__(self) -> None:
            self.is_connected = True

        def has_live_process(self) -> bool:
            return True

        async def connect(self) -> None:
            self.is_connected = True

        async def send_request(self, request: dict, **kwargs: object) -> dict:
            nonlocal send_calls
            send_calls += 1
            raise AssertionError("probe must not call send_request on live persistent session")

        async def close(self) -> None:
            self.is_connected = False

    fake = FakePersistent()
    manager._transports["playwright"] = fake  # type: ignore[assignment]
    manager._create_transport = lambda _cfg: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("probe must not create a new transport")
    )

    await manager.probe_server("playwright")
    assert send_calls == 0
    assert manager._transports["playwright"] is fake
