---
title: Hybrid search
status: shipped
sources:
  - src/mcp_router/index/search.py
  - src/mcp_router/services/aliases.py
  - src/mcp_router/services/intent_mapping.py
  - src/mcp_router/services/search_telemetry.py
  - src/mcp_router/services/server_search.py
  - src/mcp_router/http_server.py
related:
  - index.md
  - proxy-call.md
---

# Hybrid search

`ToolSearch` (`index/search.py`) is the ranking brain behind MCP tool
`search_tools`. The HTTP adapter in `http_server.py` only wires settings,
logs telemetry, and shapes the JSON response.

## Pipeline

1. Detect server names from the query (`intent_mapping.detect_servers_from_query`).
2. Match RU/EN synonyms via `AliasCatalog` (`services/aliases.py`).
3. Vector search (if an embedding client is configured) + FTS/BM25 on SQLite.
4. Merge with **RRF** (default agent-visible ranking). Optional **shadow**
   weighted merge when `SHADOW_RANKER=true` — logged only; agents still see RRF.
5. Boosts: server name in query (`server_boost`), alias hit (`alias_boost`).
6. If both channels are empty → keyword fallback over live `tools/list` cache.

`search_servers` is a thinner path (`services/server_search.py`): registry /
index lookup for downstream **servers**, not tools.

## Health quarantine (WOR-1092)

When `HEALTH_QUARANTINE_ENABLED=true` (default), the last
`downstream_health.failed_servers` snapshot:

- **drops** those servers' tools from `search_tools` results
- **marks** them on `search_servers` with `healthy=false`, `day_path=skip`
- **blocks** `call_server_tool` with `failure_kind=server_unhealthy`

Unknown / not-yet-probed servers are not quarantined. Rollback:
`HEALTH_QUARANTINE_ENABLED=false` (probes and `/status` stay unchanged).

## Telemetry

`search_telemetry` records `search_log` rows (query, hits, `search_id`) and
links later `call_server_tool` / `review` / `feedback` via audit ids. Misses
and aliases feed the search-quality loop without changing the public tool
surface.

## Boundaries

Search may use `services`, `storage`, and `embedding` types. It must **not**
import `http_server` or `cli` (enforced by import-linter).
