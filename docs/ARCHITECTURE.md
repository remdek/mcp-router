# mcp-router architecture

Standalone MCP proxy: clients connect by URL; downstream servers and secrets stay on the router host. Source of truth for the live design is `src/mcp_router/**` (see also focused fragments in `docs/context/`).

## Purpose

Instead of loading every downstream tool into the agent context, the client sees a small router surface and discovers/calls tools on demand:

| Tool | Role |
|------|------|
| `search_tools` | Hybrid search over indexed tools (RU/EN) |
| `search_servers` | Search registered downstream **servers** |
| `call_server_tool` | Proxy `tools/call` to a named server |
| `get_router_status` | Index / embeddings / downstream probe snapshot |
| `feedback` / `review` | Local quality intake (no downstream call) |

## Entry points

| Command | Module | Transport |
|---------|--------|-----------|
| `mcp-router serve` | `cli.py` → `http_server:app` | HTTP (FastAPI + Uvicorn) |
| `mcp-router index-tools` | `cli.py` → `ToolIndexer` | Offline SQLite rebuild |

Clients use Streamable HTTP at `/mcp` (optional strict surface `/mcp/v2`). There is **no** stdio MCP entry for the agent host — URL-only.

## Runtime diagram

```
┌─────────────────────────────────────────────────────────────┐
│  AI Client (Cursor / Claude Desktop / …)                    │
│  Tools: search_*, call_server_tool, get_router_status, …    │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP Streamable HTTP  (/mcp|/mcp/v2)
┌──────────────────────────▼──────────────────────────────────┐
│  http_server + http_mcp_tools + middleware                  │
│  ├── search  → ToolSearch (hybrid)                          │
│  ├── call    → ServerManager.call_tool()                    │
│  └── status  → index + health_probe + quarantine            │
└──────────┬───────────────────────────────┬────────────────────┘
           │                               │
           ▼                               ▼
┌──────────────────────┐      ┌───────────────────────────────┐
│  SQLite (tools.db)   │      │  ServerManager + Transports   │
│  FTS5 BM25 +         │      │  stdio / http / sse / ws      │
│  sqlite-vec (or      │      └───────────┬───────────────────┘
│  cosine fallback)    │                  │
└──────────────────────┘                  ▼
                               ┌────────────────────────────┐
                               │  Downstream MCP servers    │
                               │  (config/registry.json)    │
                               └────────────────────────────┘
```

## Package layout

```text
http_server / http_middleware / http_mcp_tools / cli   # adapters
        ↓
services / index                                         # application
        ↓
storage / transports / embedding / models                # leaves
```

Import direction is enforced by import-linter (`make lint-imports`). Details: `docs/context/import-boundaries.md`.

### Key modules

| Area | Path | Responsibility |
|------|------|----------------|
| Composition root | `http_server.py`, `app_state.py` | Lifespan, `/health` `/status` `/admin/reindex`, MCP mount |
| MCP tools | `http_mcp_tools.py` | Tool handlers; resolve state from the HTTP app |
| Registry | `services/registry_loader.py`, `registry_hot_reload.py`, `env_loader.py` | Load JSON, `${VAR}` substitution, optional poll reload |
| Proxy | `services/server_manager.py`, `transports/*` | Connect on demand; faithful `tools/call` relay |
| Index | `index/extractor.py`, `indexer.py` | `tools/list` → embeddings → SQLite rewrite |
| Search | `index/search.py` | Vector + FTS → **RRF** (optional shadow weighted log) |
| Storage | `storage/sqlite_*.py` | Schema, FTS, vector ANN / brute-force cosine |
| Embeddings | `embedding/*` | OpenAI-compatible remote **or** local fastembed |
| Health | `health_probe.py`, `health_quarantine.py` | Downstream probes; drop/block unhealthy servers |
| Strict | `strict_policy.py`, `DualMcpSurfaceMiddleware` | Allow-list on `/mcp/v2` |

## Hybrid search (runtime)

1. Client calls `search_tools(query=…)`.
2. Detect servers from query (`intent_mapping`) + alias boosts (`aliases`).
3. Vector ANN (sqlite-vec, else capped cosine) + FTS5 BM25 on SQLite.
4. Merge with **RRF** (agent-visible). Optional `SHADOW_RANKER` logs weighted merge only.
5. Apply health quarantine / strict filter; if both channels empty → keyword fallback on live `tools/list` cache.
6. Client calls `call_server_tool(server_name, tool_name, arguments)` → `ServerManager` proxies unchanged args.

See `docs/context/search.md`.

## Indexing

Triggered by `mcp-router index-tools`, startup reindex (`REINDEX_ON_START` / background), registry hot-reload, or `POST /admin/reindex`.

1. Load `registry.json` → `ServerManager`.
2. `ToolExtractor`: bounded parallel `tools/list` → `text_content` (+ optional aliases).
3. Embed via configured provider (or skip vectors → keyword/FTS-only / degraded).
4. `SqliteStore` rebuilds FTS + vector tables; per-server failures recorded on `IndexStatus`.

See `docs/context/index.md`.

## Registry and deploy

- Live config: `config/registry.json` (**gitignored**), path `REGISTRY_PATH`.
- Public template: `config/registry.json.example` (`${VAR}` only).
- Shape: `mcpServers` + optional `groups` / `strictProfile` (Pydantic `models/registry.py`).
- Docker Compose publishes HTTP; secrets stay in `.env` on the router host.

See `docs/context/registry.md` and `docs/context/proxy-call.md`.

## What this is not

This package is **not** the old graphindex / FalkorDB / `code-index-router` stack.
