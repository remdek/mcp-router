"""STDIO transport for MCP protocol via subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Literal

from mcp_router.reap import reap_untracked_zombies, track_child_pid, untrack_child_pid
from mcp_router.transports.base import BaseTransport

logger = logging.getLogger(__name__)

SessionMode = Literal["oneshot", "persistent"]
_DEFAULT_PERSISTENT_IDLE_SEC = 300.0


async def _close_stdio_process(
    process: asyncio.subprocess.Process,
    *,
    stderr_task: asyncio.Task[None] | None = None,
) -> None:
    """Close pipes, kill process group if needed, and wait. Idempotent; never raises."""
    if stderr_task is not None and not stderr_task.done():
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task

    stdin = process.stdin
    if stdin is not None:
        with contextlib.suppress(Exception):
            if not stdin.is_closing():
                stdin.close()
            await stdin.wait_closed()

    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    pid = process.pid
    if process.returncode is None and pid is not None:
        # Onesots use start_new_session=True so killpg covers npx/sh grandchildren.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await process.wait()
    if pid is not None:
        untrack_child_pid(pid)
    # killpg can leave npx/sh grandchildren as zombies reparented to us (PID 1 /
    # subreaper); reap them immediately so probes do not accumulate defunct PIDs.
    with contextlib.suppress(Exception):
        reap_untracked_zombies()


async def _spawn_stdio_process(
    command_path: str,
    args: list[str],
    *,
    env: dict[str, str],
    cwd: str | None,
) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        command_path,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    if process.pid is not None:
        track_child_pid(process.pid)
    return process


class StdioTransport(BaseTransport):
    """STDIO transport using subprocess for local MCP servers.

    ``session_mode=oneshot`` (default): spawn → initialize → one request → exit.
    ``session_mode=persistent``: keep one process alive across requests (browser MCP).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.command = config.get("command")
        self.args = config.get("args", [])
        self.env = config.get("env", {})
        working_dir = config.get("working_directory")
        self.working_directory = None if working_dir in (None, "null") else working_dir
        default_timeout = 120.0 if self.command == "docker" else 30.0
        self.timeout = float(config.get("timeout", default_timeout))
        mode = config.get("session_mode") or "oneshot"
        if mode not in ("oneshot", "persistent"):
            raise ValueError(f"Invalid session_mode: {mode!r}")
        self.session_mode: SessionMode = "persistent" if mode == "persistent" else "oneshot"
        idle = config.get("idle_timeout_sec")
        if idle is None:
            self.idle_timeout_sec = (
                _DEFAULT_PERSISTENT_IDLE_SEC if self.session_mode == "persistent" else 0.0
            )
        else:
            self.idle_timeout_sec = float(idle)
        if not self.command:
            raise ValueError("STDIO transport requires 'command' in config")

        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_chunks: list[str] = []
        self._session_extra_env: dict[str, str] = {}
        self._initialized = False
        self._io_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used_monotonic = 0.0

    def has_live_process(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def connect(self) -> None:
        self._connected = True

    async def send_request(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not self._connected:
            await self.connect()
        if self.session_mode == "persistent":
            async with self._io_lock:
                return await self._send_persistent(request, **kwargs)
        return await self._send_oneshot(request, **kwargs)

    async def _send_oneshot(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        extra_env = kwargs.get("extra_env") or {}
        command_path = self._resolve_command()
        cwd = self._resolve_cwd()
        full_env = os.environ.copy()
        full_env.update(self.env)
        full_env.update(extra_env)

        method = request.get("method", "")
        timeout = self.timeout * 2 if method == "tools/call" else self.timeout

        process = await _spawn_stdio_process(command_path, self.args, env=full_env, cwd=cwd)
        stderr_task: asyncio.Task[None] | None = None
        try:
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            async def _drain_stderr() -> None:
                assert process.stderr is not None
                while True:
                    chunk = await process.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk.decode("utf-8", errors="ignore"))

            stderr_task = asyncio.create_task(_drain_stderr())

            async def _write_line(message: dict[str, Any]) -> None:
                assert process.stdin is not None
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()

            async def _read_response_line() -> str:
                assert process.stdout is not None
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                if not line:
                    raise RuntimeError("MCP stdio server closed stdout before responding")
                decoded = line.decode("utf-8", errors="ignore")
                stdout_chunks.append(decoded)
                return decoded

            await _write_line(self._init_request())
            await _read_response_line()
            await _write_line(self._initialized_notification())
            await _write_line(request)

            request_id = request.get("id")
            parsed: dict[str, Any] | None = None
            while True:
                line = await _read_response_line()
                parsed = self._parse_jsonrpc_line(line, request_id)
                if parsed is not None:
                    if process.stdin is not None and not process.stdin.is_closing():
                        process.stdin.close()
                    break

            await asyncio.wait_for(process.wait(), timeout=5.0)
            await stderr_task
            stderr_task = None

            stderr = "".join(stderr_chunks).strip()
            if stderr and ("error" in stderr.lower() or "ERROR" in stderr):
                logger.warning("stdio stderr from %s: %s", self.command, stderr[:500])
            assert parsed is not None
            return parsed
        except TimeoutError as exc:
            raise RuntimeError(f"MCP stdio timeout ({timeout}s)") from exc
        finally:
            await _close_stdio_process(process, stderr_task=stderr_task)

    async def _send_persistent(self, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        extra_env = dict(kwargs.get("extra_env") or {})
        if self.has_live_process() and extra_env != self._session_extra_env:
            await self._shutdown_process()

        if not self.has_live_process():
            await self._spawn_and_initialize(extra_env)

        method = request.get("method", "")
        timeout = self.timeout * 2 if method == "tools/call" else self.timeout
        assert self._process is not None

        try:
            await self._write_line(request)
            request_id = request.get("id")
            while True:
                line = await self._read_response_line(timeout)
                parsed = self._parse_jsonrpc_line(line, request_id)
                if parsed is not None:
                    self._touch_idle()
                    return parsed
        except Exception:
            await self._shutdown_process()
            raise

    async def _spawn_and_initialize(self, extra_env: dict[str, str]) -> None:
        command_path = self._resolve_command()
        cwd = self._resolve_cwd()
        full_env = os.environ.copy()
        full_env.update(self.env)
        full_env.update(extra_env)

        process = await _spawn_stdio_process(command_path, self.args, env=full_env, cwd=cwd)
        self._process = process
        self._session_extra_env = dict(extra_env)
        self._stderr_chunks = []
        self._initialized = False

        async def _drain_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_chunks.append(chunk.decode("utf-8", errors="ignore"))

        self._stderr_task = asyncio.create_task(_drain_stderr())

        try:
            init_timeout = self.timeout
            await self._write_line(self._init_request())
            await self._read_response_line(init_timeout)
            await self._write_line(self._initialized_notification())
            self._initialized = True
            self._touch_idle()
            logger.info(
                "stdio persistent session started: command=%s pid=%s",
                self.command,
                process.pid,
            )
        except Exception:
            await self._shutdown_process()
            raise

    async def _write_line(self, message: dict[str, Any]) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def _read_response_line(self, timeout: float) -> str:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError as exc:
            raise RuntimeError(f"MCP stdio timeout ({timeout}s)") from exc
        if not line:
            raise RuntimeError("MCP stdio server closed stdout before responding")
        return line.decode("utf-8", errors="ignore")

    def _touch_idle(self) -> None:
        self._last_used_monotonic = time.monotonic()
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None
        if self.session_mode != "persistent" or self.idle_timeout_sec <= 0:
            return

        async def _idle_close() -> None:
            try:
                await asyncio.sleep(self.idle_timeout_sec)
                async with self._io_lock:
                    if not self.has_live_process():
                        return
                    idle_for = time.monotonic() - self._last_used_monotonic
                    if idle_for + 0.05 < self.idle_timeout_sec:
                        return
                    logger.info(
                        "stdio persistent idle timeout (%.0fs): closing %s",
                        self.idle_timeout_sec,
                        self.command,
                    )
                    await self._shutdown_process()
            except asyncio.CancelledError:
                return

        self._idle_task = asyncio.create_task(_idle_close())

    async def _shutdown_process(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
        self._idle_task = None

        process = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._stderr_task = None
        self._initialized = False
        self._session_extra_env = {}

        if process is not None:
            stderr = "".join(self._stderr_chunks).strip()
            if stderr and ("error" in stderr.lower() or "ERROR" in stderr):
                logger.warning("stdio stderr from %s: %s", self.command, stderr[:500])
            await _close_stdio_process(process, stderr_task=stderr_task)
        self._stderr_chunks = []

    @staticmethod
    def _init_request() -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-router", "version": "0.1.0"},
            },
        }

    @staticmethod
    def _initialized_notification() -> dict[str, Any]:
        return {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}

    def _resolve_command(self) -> str:
        if not self.command:
            raise RuntimeError("command not set")
        if os.path.isabs(self.command) or os.path.exists(self.command):
            return str(self.command)
        resolved = shutil.which(self.command)
        return str(resolved or self.command)

    def _resolve_cwd(self) -> str | None:
        if self.working_directory:
            p = Path(self.working_directory)
            return str(p if p.is_absolute() else Path.cwd() / p)
        return None

    def _parse_jsonrpc_line(self, line: str, request_id: Any) -> dict[str, Any] | None:
        line = line.strip()
        if not line or '"notifications/' in line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None
        if msg.get("jsonrpc") != "2.0":
            return None
        if request_id is not None and msg.get("id") == request_id and "result" in msg:
            result = msg["result"]
            return result if isinstance(result, dict) else {"value": result}
        if "error" in msg and (request_id is None or msg.get("id") == request_id):
            err = msg["error"]
            raise RuntimeError(f"MCP error [{err.get('code')}]: {err.get('message')}")
        return None

    def _parse_response(self, stdout: str, request_id: Any) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            parsed = self._parse_jsonrpc_line(line, request_id)
            if parsed is not None:
                return parsed
        raise RuntimeError("No valid JSON-RPC response in stdio output")

    async def close(self) -> None:
        async with self._io_lock:
            await self._shutdown_process()
        self._connected = False
