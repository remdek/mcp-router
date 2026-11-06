---
title: Registry and configuration
status: shipped
sources:
  - src/mcp_router/services/registry_loader.py
  - src/mcp_router/services/registry_hot_reload.py
  - src/mcp_router/services/env_loader.py
  - src/mcp_router/models/registry.py
  - src/mcp_router/models/settings.py
  - src/mcp_router/app_state.py
  - src/mcp_router/config.py
  - config/registry.json.example
related:
  - proxy-call.md
  - index.md
---

# Registry

Downstream MCP servers are declared in `config/registry.json` (path from
`REGISTRY_PATH` / `Settings`). Shape: `mcpServers` plus optional `groups`
and `strictProfile` (same JSON layout historically used by graphindex).

Public SoT is `config/registry.json.example` (`${VAR}` placeholders). Live
`config/registry.json` is gitignored so deploy/LAN hosts are not published.

## Load path

1. `registry_loader.load_registry` reads JSON and runs `${VAR}` substitution
   from `.env` + process env (`substitute_env_vars`).
2. Payload validates as Pydantic `Registry` (`models/registry.py`).
3. `ServerManager.load_registry` skips `disabled` entries and builds
   transports on demand.

Credentials stay on the router host — clients only see the router URL (and
optional Bearer). Error paths redact bearer/api-key shaped secrets.

## Lifecycle modes (WOR-1541)

Per-server product fields (pi-mcp pattern), applied by `ServerManager` when
building stdio transports. Connect is always **lazy** at the gateway: registry
load never spawns processes; the first `tools/list` / `tools/call` connects.

| `lifecycle` | Behavior | Default `idle_timeout_sec` |
|-------------|----------|----------------------------|
| unset | LAN defaults: stdio **oneshot** (spawn per request); browser allowlist `playwright` / `chrome-devtools` → **persistent** with 300s idle | transport default |
| `lazy` | Reuse one stdio process across calls; disconnect after idle TTL | **600** (10 min) |
| `keep-alive` | Reuse process; **no** idle shutdown (stay resident after first use) | **0** |
| `lazy-keep-alive` | Same residency as `keep-alive` (gateway does not connect-at-startup) | **0** |

Optional `idle_timeout_sec` overrides the default for that mode (`0` = never
idle-close). Low-level `session_mode` (`oneshot` / `persistent`) remains for
legacy configs when `lifecycle` is unset.

Example:

```json
"playwright": {
  "command": "npx",
  "args": ["-y", "@playwright/mcp@latest"],
  "lifecycle": "lazy-keep-alive",
  "description": "Browser MCP — keep Chromium session after first call"
},
"github": {
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "lifecycle": "lazy",
  "idle_timeout_sec": 300
}
```

Unset `lifecycle` on existing LAN registries is unchanged (oneshot / browser
allowlist). Health probes share the stdio pool lock and never tear a live
keep-alive process — see `proxy-call.md`.

## Hot reload (U7)

With `REGISTRY_HOT_RELOAD=true` (default), `RegistryHotReloader` polls
`registry.json` (`REGISTRY_POLL_INTERVAL_SEC`). On mtime/size change the
adapter reloads the file, calls `ServerManager.apply_registry_update`
(disconnect removed/changed servers, clear tool cache), refreshes
`strict_policy` from `strictProfile`, and schedules a background reindex —
no process restart.

Runtime objects live in `AppState` (`app_state.py`), injected into FastAPI
via `Depends(get_app_state)` and shared by MCP tools.

## Settings

`models/settings.py` / `config.py` hold process config: index DB path,
embedding mode, reindex flags, registry hot-reload, strict MCP env overrides,
alias path, shadow ranker. Registry file content is separate from Settings.

## Boundaries

`models` is a pure leaf (no imports of services/index/http). Registry loading
lives in `services` and is used by both `http_server` and `cli`.
