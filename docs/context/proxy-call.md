---
title: Proxy call (faithful relay)
status: shipped
sources:
  - src/mcp_router/http_server.py
  - src/mcp_router/http_mcp_tools.py
  - src/mcp_router/services/server_manager.py
  - src/mcp_router/services/responses.py
  - src/mcp_router/services/output_guard.py
  - src/mcp_router/services/strict_policy.py
  - src/mcp_router/services/health_probe.py
  - src/mcp_router/services/failure_backoff.py
  - src/mcp_router/services/feedback.py
  - src/mcp_router/services/feedback_contract.py
  - src/mcp_router/transports/base.py
  - src/mcp_router/transports/stdio.py
  - src/mcp_router/transports/http.py
  - src/mcp_router/transports/sse.py
  - src/mcp_router/transports/websocket.py
related:
  - registry.md
  - search.md
---

# Proxy call

`call_server_tool` in `http_mcp_tools.py` is a thin adapter: strict allow-list
check → registry presence → **failure backoff** / health quarantine →
`ServerManager.call_tool` → normalize result → optional **output guard** or
emit a typed remediation error. It does **not** re-model downstream APIs.

## Faithful relay

`ServerManager` owns transport lifecycle for each registered server. Transport
choice follows registry config (`stdio` / `http` / `sse` / `websocket`). The
manager passes tool name + arguments through unchanged; only env injection
(for stdio) and connection setup are router-owned.

`services/responses.py` normalizes MCP tool results into a stable JSON shape
and classifies failures (`failure_kind`, `reconnect_hint`, `next_action`)
without leaking secrets or raw headers.

## Output guard (context budget Layer C)

Large successful (and tool-level error) results are capped so one huge
downstream reply cannot inflate the agent context:

1. Measure extracted `result.text` (or serialized `result`) against
   `OUTPUT_GUARD_MAX_BYTES` (default **50 KiB**) and
   `OUTPUT_GUARD_MAX_LINES` (default **2000**).
2. If over limit: write the **full** call JSON under
   `data/call_artifacts/{call_id}.json` (or `OUTPUT_GUARD_DIR`), replace
   `result` with a head preview + truncation notice, and set
   `output_guard` (`truncated`, `full_output_path`, `artifact_id`, sizes).
3. Typed remediation envelopes (`failure_kind`, …) are **not** truncated —
   they stay short and actionable.
4. Rollback: `OUTPUT_GUARD_ENABLED=false`.

See ADR 0001 and `services/output_guard.py`.

## Strict surface

`/mcp/v2` uses `strict_policy` to allow-list servers/tools (and optional
read-only). Denials return `failure_kind=strict_denied`. Full `/mcp` is
unchanged.

## Health + failure backoff

`health_probe` periodically checks downstream reachability for
`get_router_status` / `/status`. HTTP/SSE/WebSocket probes use a one-shot
transport (call-path pool untouched). Stdio probes reuse the pooled transport
under a per-server lock and skip spawning when a call is already in flight so
a second subprocess never races the call path (A3). Persistent browser stdio
(`session_mode=persistent`, allowlist `playwright` / `chrome-devtools`) keeps
one process across calls; a live process satisfies the probe without a second
spawn or tearing the session (WOR-874).

**Failure backoff** (WOR-1539) is the fast agent-visible layer: a failed
connect/call records a sanitized reason for ~60s. While active,
`call_server_tool` returns `failure_kind=failure_backoff` with
`failed Ns ago: <reason>` instead of silent retries. Quarantine remains the
probe-based day-path policy; both can apply, backoff carries the fresher
reason when present.

## Boundaries

`services` and `transports` must not import `http_server` or `cli`.
Transports are a leaf: no imports of services, index, or storage.
