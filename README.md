# mcp-router

MCP proxy with lazy tool loading and hybrid search (vector + BM25). Clients connect via URL only — no local MCP install.

**Vector search:** prefers sqlite-vec ANN. If sqlite-vec is missing or fails, falls back to brute-force cosine over stored embeddings, capped at **5000** rows (`BRUTE_FORCE_VECTOR_SCAN_LIMIT` in `storage/sqlite_store.py`) with a warning log when the index is larger. Install sqlite-vec for unbounded ANN on big indexes.

## Quick start (Docker)

```bash
cp .env.example .env
cp config/registry.json.example config/registry.json
# Edit .env (HOST_PORT, embedding URL, API keys) and config/registry.json

docker compose up --build -d

# Happy path (defaults: HOST_PORT=8000 → http://localhost:8000)
curl http://localhost:8000/health
curl http://localhost:8000/status
```

Change the published port with `HOST_PORT` in `.env` (container still listens on `MCP_ROUTER_PORT`, default `8000`). Secrets and downstream URLs stay in `.env` and are substituted into `config/registry.json` as `${VAR}`.

## Cursor config

Replace `<ROUTER_HOST>` with your router hostname or IP (e.g. `localhost` for local Docker).

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://<ROUTER_HOST>:8000/mcp"
    }
  }
}
```

With Bearer auth:

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://<ROUTER_HOST>:8000/mcp",
      "headers": {
        "Authorization": "Bearer <ROUTER_API_KEY>"
      }
    }
  }
}
```

## Router tools

| Tool | Description |
|------|-------------|
| `get_router_status` | Health, index/reindex state, embedding mode, downstream probes, reconnect hints |
| `search_servers` | Discover registered downstream servers (not tools); returns `search_id` |
| `search_tools` | Hybrid search with confidence + per-server grouping; returns `search_id` |
| `call_server_tool` | Proxy call with normalized result and typed remediation errors; returns `call_id` |
| `review` | Rate a prior `call_id` or `search_id` (`useful` / `partly` / `not_useful` / `not_sure`) |
| `feedback` | Local quality/friction/other report; optional `call_ids` / `search_ids` |

### Feedback / review (local only)

After `search_tools` / `call_server_tool`, agents can leave structured signals for the ranker:

- `review(usefulness=..., call_id=...)` or `review(..., search_id=...)` — exactly one target; optional `reason` (≤200 chars). Idempotent per target (`already_reviewed`).
- `feedback(category, message, call_ids?, search_ids?)` — categories: `quality`, `friction`, `other`. Missing audit refs do not reject; `verified_*` lists which ones joined.

Writes go to SQLite (`call_audit`, `reviews`, `feedback`) joined to `search_log`. **No downstream MCP call.** Queries/reasons redact bearer/API-key shaped secrets.

### Typed remediation errors

When `call_server_tool` fails (or search cannot run / index is degraded), the JSON includes actionable fields **without secrets or raw headers**:

| Field | Meaning |
|-------|---------|
| `failure_kind` | One of: `downstream_unreachable`, `auth_failed`, `index_degraded`, `tool_not_found`, `timeout` |
| `server_name` | Downstream server (when known) |
| `reconnect_hint` | What is wrong / where to look |
| `next_action` | Concrete next step for the agent (also mirrored as `retry_hint`) |
| `error_source` | Legacy coarse bucket: `router` / `downstream` / `tool` |

`search_tools` sets the same remediation fields when the index is pending, indexing, in error, or empty (`tool_count == 0`). Successful searches stay `ok: true` and only add hints when degraded.

Credentials stay on the router host (`.env` / registry headers) — error text redacts `Bearer`, `api_key`, and similar tokens.

## Registry format

Server-side `config/registry.json` — compatible with graphindex format (`mcpServers` + optional `groups`). Downstream servers must be reachable from the router host/container.

**Public SoT:** `config/registry.json.example` (placeholders `${VAR}` only). Live `config/registry.json` is gitignored — copy the example and point URLs/secrets at your host via `.env`. Do not commit a deploy registry with LAN IPs or internal hostnames.

See [docs/PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md) before changing GitHub visibility to public.

## CLI

```bash
pip install -e .
mcp-router serve              # HTTP server on :8000
mcp-router index-tools        # Rebuild SQLite index
```

## Development

**После клонирования обязательно один раз:**

```bash
./scripts/setup-dev.sh
```

Скрипт создаёт `.venv`, ставит `pip install -e ".[dev]"` и **`pre-commit install`** — без этого git-хук не появится и `git commit` не запустит проверки. Конфиг `.pre-commit-config.yaml` в репозитории сам по себе хуки не устанавливает.

Или вручную:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install --install-hooks
```

Проверки:

```bash
make check              # pre-commit (lint, typecheck, import-linter, tests)
make lint-imports       # import-linter layer contracts
make context-drift      # map changed sources → docs/context fragments
make test               # pytest
ruff check src tests    # lint
ruff format src tests   # format
mypy                    # typecheck
```

Pydantic models live in `src/mcp_router/models/` (`Registry`, `Settings`, `ToolMetadata`, etc.). Registry JSON is validated on load via `services/registry_loader.py`.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST_PORT` | `8000` | Host port published by `docker compose` (`HOST_PORT:MCP_ROUTER_PORT`) |
| `MCP_ROUTER_HOST` | `0.0.0.0` | Bind address inside the process/container |
| `MCP_ROUTER_PORT` | `8000` | Listen port inside the process/container |
| `REGISTRY_PATH` | `config/registry.json` | MCP server registry |
| `INDEX_DB_PATH` | `data/tools.db` | SQLite index path |
| `ALIASES_PATH` | `config/aliases.json` | RU/EN server/tool synonyms for index + boost |
| `SHADOW_RANKER` | `false` | Log weighted shadow ranking; agent still sees baseline RRF |
| `ALIAS_BOOST` | `1.35` | Score multiplier when query matches an alias |
| `EMBEDDING_URL` | — | If set **with** `EMBEDDING_MODEL`, use remote OpenAI-compatible API |
| `EMBEDDING_MODEL` | — | Remote embedding model (required with `EMBEDDING_URL`) |
| `LOCAL_EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Local CPU model when URL not set |
| `ROUTER_API_KEY` | — | Optional Bearer token for `/mcp` and `/mcp/v2` |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins (e.g. `https://app.example.com,http://localhost:3000`) |
| `STRICT_MCP_ENABLED` | `true` | Mount strict surface at `/mcp/v2` (rewrites to `/mcp` + allow-list) |
| `STRICT_ALLOWLIST` | — | CSV (`hindsight,searxng:web_search`), JSON object, or path to JSON file |
| `STRICT_READ_ONLY` | `false` | On `/mcp/v2`, only tools with `annotations.readOnlyHint=true` |
| `REINDEX_ON_START` | `true` | Reindex on startup |
| `REINDEX_BACKGROUND` | `true` | Non-blocking reindex |
| `REGISTRY_HOT_RELOAD` | `true` | Poll `registry.json` and reload without restart |
| `REGISTRY_POLL_INTERVAL_SEC` | `2.0` | Hot-reload poll interval (seconds) |
| `HEALTH_PROBE_ENABLED` | `true` | Periodic downstream `tools/list` probes |
| `HEALTH_QUARANTINE_ENABLED` | `true` | Day-path: mark/skip failed servers; block their calls (WOR-1092) |

### Public deployment (security)

Default CORS is `ALLOWED_ORIGINS=*` and DNS-rebinding protection is off — fine for a trusted LAN, **not** for a host reachable from the public internet.

If you expose mcp-router beyond the LAN:

1. Set `ROUTER_API_KEY` and require `Authorization: Bearer …` on clients (see Cursor config above).
2. Set `ALLOWED_ORIGINS` to an explicit comma-separated allowlist of browser origins (do not leave `*`).
3. Prefer `/mcp/v2` with `STRICT_ALLOWLIST` for untrusted clients so they only reach a curated subset of tools.

## Endpoints

| Path | Description |
|------|-------------|
| `GET /health` | Liveness |
| `GET /status` | tool_count, last_indexed_at, embedding status, search_quality, downstream_health probes, mcp_surfaces |
| `POST /mcp` | MCP Streamable HTTP — **full** surface (default; unchanged) |
| `POST /mcp/v2` | MCP Streamable HTTP — **strict** surface (allow-list / optional read-only) |
| `POST /admin/reindex` | Trigger background reindex |

### When to use `/mcp/v2`

Point safe / untrusted clients at `/mcp/v2` when they should only reach a curated subset of downstream servers/tools. Keep agents that need the full LAN registry on `/mcp`.

Configure allow-list via env (wins) or `strictProfile` in `registry.json`:

```bash
# CSV: whole servers and/or server:tool keys
STRICT_ALLOWLIST=hindsight,searxng,graphiti:search_nodes

# or JSON
STRICT_ALLOWLIST='{"servers":["hindsight","searxng"],"tools":[],"read_only":true}'
```

```json
{
  "strictProfile": {
    "servers": ["hindsight", "searxng"],
    "tools": ["graphiti:search_nodes"],
    "readOnly": false
  },
  "mcpServers": { }
}
```

Calls outside the allow-list return typed `failure_kind=strict_denied` (same remediation shape as other router errors). Empty allow-list on `/mcp/v2` denies all `call_server_tool` until configured.

## Troubleshooting

- **Empty search results** — check `/status`: wait for `index_status: ready`, verify `EMBEDDING_URL` or use keyword fallback.
- **Downstream tool fails** — ensure server is in registry and env vars (`${VAR}`) are set in `.env`.
- **401 Unauthorized** — set matching `Authorization: Bearer` header in Cursor config.
- **stdio servers in Docker** — npx-based servers need Node in the image (included in Dockerfile).
- **`entrypoint.sh: no such file or directory` (Windows)** — CRLF line endings in shell scripts. Pull latest (`.gitattributes` + Dockerfile fix) and rebuild: `docker compose build --no-cache && docker compose up -d`.
- **Embedding / reindex errors** — if `EMBEDDING_URL` + `EMBEDDING_MODEL` set, uses remote API; otherwise local CPU (fastembed). Check `get_router_status` for mode and health.
- **Desktop-only MCP servers (browsermcp, etc.)** — `lsof`/`xargs` fixed in Docker image (`procps`, `findutils`). Real requirement: Chrome extension + WebSocket on port 9009 (`9009:9009` in compose). See [docs/BROWSER_MCP_DOCKER.md](docs/BROWSER_MCP_DOCKER.md). For headless-in-container use `chrome-devtools-mcp`.
- **Cursor `fetch failed` / ECONNREFUSED** — check container is up (`docker ps`), published port matches `HOST_PORT` in `.env` (default `8000` → `http://localhost:8000/mcp`), and `/status` shows `index_status: ready`.

## Docs

- [docs/context/](docs/context/) — design fragments (search, proxy call, registry, index) with `sources:` + import-linter contracts
- [docs/PLAN.md](docs/PLAN.md) — implementation plan
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — graphindex reference analysis
- [docs/BROWSER_MCP_DOCKER.md](docs/BROWSER_MCP_DOCKER.md) — browser MCP servers in Docker
- [docs/PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md) — before GitHub `visibility=public`
