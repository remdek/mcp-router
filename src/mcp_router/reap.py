"""Reap orphaned child zombies without racing asyncio-tracked subprocesses.

When mcp-router runs as PID 1 (or otherwise inherits grandchildren from ``npx``/
``npm exec`` shell wrappers), exited shells that their parent failed to wait on
are reparented here and stay as zombies until someone calls ``waitpid``.

We only wait on zombie children whose PIDs are **not** in the tracked set
(stdio transport registers direct children while asyncio still owns them).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_tracked_pids: set[int] = set()
_reaper_task: asyncio.Task[None] | None = None


def track_child_pid(pid: int) -> None:
    if pid > 0:
        _tracked_pids.add(pid)


def untrack_child_pid(pid: int) -> None:
    _tracked_pids.discard(pid)


def list_zombie_child_pids(parent_pid: int) -> list[int]:
    """Return PIDs of zombie children of ``parent_pid`` (Linux ``/proc``)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    found: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        # ``stat``: pid (comm) state ppid ... — comm may contain spaces/parens.
        close = raw.rfind(")")
        if close < 0:
            continue
        rest = raw[close + 2 :].split()
        if len(rest) < 2:
            continue
        state, ppid_s = rest[0], rest[1]
        if state != "Z":
            continue
        try:
            if int(ppid_s) != parent_pid:
                continue
            found.append(int(entry.name))
        except ValueError:
            continue
    return found


def reap_untracked_zombies() -> int:
    """``waitpid`` zombie children that asyncio is not tracking. Returns count."""
    reaped = 0
    me = os.getpid()
    for pid in list_zombie_child_pids(me):
        if pid in _tracked_pids:
            continue
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if waited == 0:
            continue
        reaped += 1
        logger.info("Reaped orphaned zombie child pid=%s", waited)
    return reaped


async def _reap_loop(interval_sec: float) -> None:
    while True:
        try:
            reap_untracked_zombies()
        except Exception:
            logger.exception("Zombie reaper tick failed")
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            reap_untracked_zombies()
            raise


def start_zombie_reaper(*, interval_sec: float = 5.0) -> None:
    """Start background reap loop (idempotent). Call from app lifespan."""
    global _reaper_task
    if _reaper_task is not None and not _reaper_task.done():
        return
    reap_untracked_zombies()
    _reaper_task = asyncio.create_task(
        _reap_loop(interval_sec),
        name="mcp-router-zombie-reaper",
    )
    logger.info(
        "Zombie reaper started (interval=%ss pid=%s is_pid1=%s)",
        interval_sec,
        os.getpid(),
        os.getpid() == 1,
    )


async def stop_zombie_reaper() -> None:
    global _reaper_task
    task = _reaper_task
    _reaper_task = None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    reap_untracked_zombies()
