# Окончательный план: mcp-router (новое приложение)

Целевая папка: `/home/msm/git-repos/mcp-router`
Подход: **новое приложение**, вдохновлённое graphindex mcp-router — не вынос и не fork
Деплой: Docker Compose на вашем хосте (замените `<ROUTER_HOST>` на hostname/IP)
Клиент: **только URL** в конфиге Cursor — ничего не ставится на машине пользователя

---

## 1. Модель использования

```
┌─────────────────────────────────────────────────────────────┐
│  Клиент (Cursor на любой машине)                            │
│  mcp.json: { "url": "http://<ROUTER_HOST>:8000/mcp" }        │
│  Устанавливается: ничего                                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP over HTTP (Streamable HTTP / SSE)
┌──────────────────────────▼──────────────────────────────────┐
│  mcp-router (Docker на <ROUTER_HOST>)                        │
│  Tools exposed: search_tools, call_server_tool              │
│  ├── hybrid search (SQLite: vector + BM25)                  │
│  └── proxy → downstream MCP servers                         │
│      config/registry.json (volume на сервере)               │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         github MCP   context7     code-analyze …
         (в контейнере router или на том же хосте)
```

**На сервере:** `config/registry.json` + `.env` (секреты, embedding url/model)
**На клиенте:** одна строка URL (+ опционально `headers` с API key)

### Подключение в Cursor

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://<ROUTER_HOST>:8000/mcp"
    }
  }
}
```

С авторизацией (рекомендуется для LAN):

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

---

## 2. Что берём из graphindex (как reference, не как код для копирования)

| Идея из graphindex | Как реализуем в новом приложении |
|--------------------|----------------------------------|
| 2 router-tools в контексте агента | `search_tools` + `call_server_tool` |
| Lazy loading downstream tools | `ServerManager` — connect on demand |
| Registry JSON с groups | Тот же формат `mcpServers` + `groups` |
| Hybrid search vector + BM25 | SQLite FTS5 + sqlite-vec, RRF merge |
| Transports stdio/http/sse/ws | Только на **сервере** router; клиент видит HTTP |
| Reindex при старте | entrypoint + background refresh |
| Intent fallback | Упрощённый словарь RU/EN |

**Не берём:** FalkorDB, code_index deps, validate_step_tools, Langfuse (опционально позже), stdio MCP entry point для клиента.

---

## 3. Улучшения относительно graphindex

| # | Улучшение | Зачем |
|---|-----------|-------|
| U1 | **URL-only клиент** | Нулевая установка на машине разработчика |
| U2 | **Streamable HTTP** (MCP 2025-03-26) + fallback SSE | Современный транспорт; Cursor поддерживает `url` |
| U3 | **SQLite** вместо FalkorDB | Один файл, без Redis-стека |
| U4 | **RRF** для hybrid search | Стабильнее weighted sum при разных шкалах scores |
| U5 | **Background reindex** | Старт сервера не блокируется; search работает на старом индексе |
| U6 | **Auth на router** | API key / Bearer — router на LAN не открыт всем |
| U7 | **Hot reload registry** | `inotify`/poll `registry.json` → reindex без restart |
| U8 | **Enriched index text** | В `text_content` добавлять RU/EN aliases, group keywords |
| U9 | **`/health` + `/status`** | tool_count, last_indexed_at, embedding_provider_ok |
| U10 | **Параллельный extract** | asyncio semaphore при index-tools (быстрее при 10+ серверах) |

### Замечания / риски

1. **stdio downstream в Docker** — npx/github и т.п. должны работать *внутри* контейнера router (Node в image) или registry на сервере указывает на HTTP/SSE upstream. Это ограничение серверной стороны, клиенту всё равно.
2. **WORKSPACE_FOLDER** — при URL-клиенте нет MCP roots от Cursor на сервер. Downstream tools, которым нужен путь проекта, должны получать `workspace_path` в arguments от агента, а не через env injection.
3. **Мультиязычный BM25** — FTS5 `unicode61` достаточен для MVP; если качество RU поиска слабое — plug-in Tantivy (U8 частично компенсирует aliases).
4. **Embedding при старте** — если LM Studio недоступен, сервер поднимается с keyword-only search и флагом `index_status: degraded` в `/status`.

---

## 4. Storage: SQLite + FTS5 + sqlite-vec

Один файл `data/tools.db` на volume.

```sql
CREATE TABLE tools (
  tool_id       TEXT PRIMARY KEY,
  server_name   TEXT NOT NULL,
  tool_name     TEXT NOT NULL,
  description   TEXT,
  text_content  TEXT NOT NULL,
  parameters    TEXT,
  tool_schema   TEXT,
  indexed_at    TEXT
);

CREATE VIRTUAL TABLE tools_fts USING fts5(
  tool_id UNINDEXED, server_name, tool_name, description, text_content,
  tokenize='unicode61'
);
-- vec0 virtual table (sqlite-vec) для ANN
```

Fallback без sqlite-vec: brute-force cosine по `tools.embedding`.
Защитный потолок **`BRUTE_FORCE_VECTOR_SCAN_LIMIT = 5000`**: сканируется не больше N строк
(с `ORDER BY tool_id`); при превышении — `WARNING` в лог. Для индексов больше N нужен sqlite-vec.

---

## 5. Гибридный поиск

1. Vector search (sqlite-vec, top 2×limit)
2. BM25 fulltext (FTS5, top 2×limit)
3. **RRF** merge: `score = Σ 1/(k + rank)`, k=60
4. Server boost ×1.2 если server detected в query
5. Fallback: keyword по registry + intent map

Параметры в `search_tools`: `query`, `limit`, `vector_weight`/`fulltext_weight` (опционально, для weighted mode).

---

## 6. Структура проекта

```
mcp-router/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── config/
│   └── registry.json          # на сервере, volume mount
├── data/
│   └── tools.db               # volume, gitignore
├── docs/
│   ├── ARCHITECTURE.md        # разбор graphindex (reference)
│   └── PLAN.md
├── scripts/entrypoint.sh
└── src/mcp_router/
    ├── config.py
    ├── http_server.py         # единственная точка входа для клиентов
    ├── mcp_protocol.py        # MCP session over Streamable HTTP / SSE
    ├── cli.py                 # index-tools, serve
    ├── embedding/openai_compatible.py
    ├── index/{extractor,indexer,search}.py
    ├── storage/sqlite_store.py
    ├── services/{server_manager,cache,env_loader,intent_mapping}.py
    └── transports/{stdio,http,sse,websocket}.py
```

**Нет** `mcp-router-mcp` stdio entry — клиенты только по URL.

---

## 7. Docker Compose (<ROUTER_HOST>)

```yaml
services:
  mcp-router:
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config:/app/config:ro
      - ./data:/app/data
    env_file: .env
    environment:
      REGISTRY_PATH: /app/config/registry.json
      INDEX_DB_PATH: /app/data/tools.db
      REINDEX_ON_START: "true"
      REINDEX_BACKGROUND: "true"
      ROUTER_API_KEY: ${ROUTER_API_KEY:-}
```

`.env` на сервере:

```env
EMBEDDING_URL=http://localhost:1234/v1
EMBEDDING_MODEL=text-embedding-multilingual-e5-small
ROUTER_API_KEY=change-me
GITHUB_PERSONAL_ACCESS_TOKEN=...
```

`entrypoint.sh`: запустить HTTP server → background `index-tools` если `REINDEX_ON_START=true`.

---

## 8. Этапы реализации

### Фаза 1 — HTTP MCP server + proxy (3 дня)

- FastAPI + MCP Streamable HTTP endpoint `/mcp` (SSE fallback `/sse`)
- `ServerManager` + transports (реализация по образцу graphindex, новый код)
- 2 tools: `search_tools` (keyword fallback), `call_server_tool`
- Auth middleware (optional Bearer)
- **Критерий:** Cursor по URL вызывает tool на downstream MCP

### Фаза 2 — Index + SQLite (2–3 дня)

- `index-tools` CLI, extractor, SQLite store
- OpenAI-compatible embeddings
- Background reindex, `/status` endpoint
- **Критерий:** restart → reindex → search находит tools

### Фаза 3 — Hybrid search (1–2 дня)

- Vector + BM25 + RRF
- Intent fallback, server boost
- Тесты RU/EN queries
- **Критерий:** `search_tools('создать задачу')` → релевантный server:tool

### Фаза 4 — Deploy + docs (1 день)

- Docker image с Node (для npx downstream)
- Деплой на `<ROUTER_HOST>`
- README: Cursor config, registry format, troubleshooting
- **Критерий:** второй клиент в LAN подключается только по URL

### Фаза 5 — Улучшения (по желанию)

- Hot reload registry
- Tantivy FTS backend
- Метрики (Prometheus `/metrics`)
- Multi-instance registry namespaces

**Итого MVP: ~7–9 дней**

---

## 9. Registry (серверный конфиг)

Формат совместим с graphindex `registry.json`. Редактируется на хосте router (`config/registry.json`, gitignored), монтируется в контейнер. Клиенты registry не видят. Публичный шаблон — `config/registry.json.example`.

Downstream серверы — только те, что доступны **с хоста router** (localhost services, HTTP APIs, stdio commands в контейнере).

---

## 10. Следующий шаг

Фаза 1: scaffold `pyproject.toml`, HTTP MCP endpoint, `ServerManager` с одним HTTP downstream (context7) + smoke test из Cursor по URL.
