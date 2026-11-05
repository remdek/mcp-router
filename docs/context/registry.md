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
