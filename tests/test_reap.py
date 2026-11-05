"""Zombie reaping for orphaned grandchildren (WOR-1030)."""

from __future__ import annotations

import asyncio
import ctypes
import os
import time
from pathlib import Path

import pytest

from mcp_router.reap import (
    list_zombie_child_pids,
    reap_untracked_zombies,
    track_child_pid,
    untrack_child_pid,
)
from mcp_router.transports.stdio import _close_stdio_process

PR_SET_CHILD_SUBREAPER = 36


def _enable_child_subreaper() -> None:
    """Become a subreaper so orphaned grandchildren reparent to this test process."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        pytest.skip(f"PR_SET_CHILD_SUBREAPER unavailable (errno={err})")


@pytest.mark.skipif(not Path("/proc").exists(), reason="/proc required")
async def test_reap_untracked_orphan_shell_zombie() -> None:
    """Double-fork orphan: parent exits without wait → zombie under subreaper."""
    _enable_child_subreaper()
    code = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    pid2 = os.fork()\n"
        "    if pid2 == 0:\n"
        "        os._exit(0)\n"
        "    time.sleep(0.05)\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "time.sleep(0.05)\n"
    )
    leader = await asyncio.create_subprocess_exec("python3", "-c", code)
    await leader.wait()

    deadline = time.monotonic() + 2.0
    zombies: list[int] = []
    while time.monotonic() < deadline:
        zombies = list_zombie_child_pids(os.getpid())
        if zombies:
            break
        await asyncio.sleep(0.05)

    assert zombies, "expected at least one orphan zombie child"
    tracked = zombies[0]
    track_child_pid(tracked)
    assert reap_untracked_zombies() == 0, "tracked zombies must not be stolen from asyncio"
    untrack_child_pid(tracked)

    reaped = reap_untracked_zombies()
    assert reaped >= 1
    assert tracked not in list_zombie_child_pids(os.getpid())


@pytest.mark.skipif(not Path("/proc").exists(), reason="/proc required")
async def test_close_stdio_kills_process_group_and_reaps() -> None:
    """Oneshot teardown must killpg the session and reap grandchild zombies."""
    _enable_child_subreaper()
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "sleep 120 & wait",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.pid is not None
    await asyncio.sleep(0.15)
    pgid = os.getpgid(proc.pid)
    await _close_stdio_process(proc)
    assert proc.returncode is not None

    # No live (or zombie) members left in the session.
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    assert not list_zombie_child_pids(os.getpid())


@pytest.mark.skipif(not Path("/proc").exists(), reason="/proc required")
def test_list_zombie_child_pids_returns_list() -> None:
    assert isinstance(list_zombie_child_pids(os.getpid()), list)


def test_track_untrack_roundtrip() -> None:
    track_child_pid(123456)
    untrack_child_pid(123456)
    untrack_child_pid(123456)
