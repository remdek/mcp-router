# Анализ mcp-router в graphindex

Источник: `/home/msm/git-repos/graphindex/src/code_index/mcp/router/`

## Назначение

MCP Router — прокси между AI-клиентом (Cursor, Claude Desktop) и набором MCP-серверов. Вместо загрузки десятков инструментов в контекст агента (~7 900 токенов для 50 tools) клиент видит **2 активных инструмента** (~200–300 токенов):

| Инструмент | Назначение |
|------------|------------|
| `search_tools` / `search_servers` | Гибридный поиск серверов и инструментов по запросу (RU/EN) |
| `call_server_tool` | Вызов любого инструмента на любом зарегистрированном сервере |

Остальные инструменты (`list_servers`, `get_server_tools`, `get_tool_details`, intent-группы и т.д.) **закомментированы** в `mcp_server.py` — доступ только через поиск + proxy.

## Точки входа

| Команда | Модуль | Транспорт |
|---------|--------|-----------|
| `code-index-router` | `server.py` | HTTP (FastAPI + uvicorn, порт 8000) |
| `code-index-router-mcp` | `mcp_server.py` | STDIO (MCP protocol для Cursor) |

Оба используют один `ServerManager` и один `registry.json`.

## Архитектура (текущая)

```
┌─────────────────────────────────────────────────────────────┐
│  AI Client (Cursor)                                         │
│  Tools in context: search_tools, call_server_tool           │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP stdio / HTTP
┌──────────────────────────▼──────────────────────────────────┐
│  mcp_server.py / server.py                                  │
│  ├── search → MCPToolSearch (hybrid)                        │
│  └── call    → ServerManager.call_tool()                    │
└──────────┬───────────────────────────────┬────────────────────┘
           │                               │
           ▼                               ▼
┌──────────────────────┐      ┌───────────────────────────────┐
│  FalkorDB (tools DB) │      │  ServerManager + Transports   │
│  MCPTool nodes       │      │  stdio / http / sse / ws      │
│  vector + fulltext   │      └───────────┬───────────────────┘
└──────────────────────┘                  │
                                            ▼
                               ┌────────────────────────────┐
                               │  MCP Servers (registry.json) │
                               │  code-analyze, code-tasks,   │
                               │  github, context7, ...       │
                               └────────────────────────────┘
```

## Ключевые модули

### `services/server_manager.py`

- Загружает `registry.json` → `mcpServers`
- Создаёт транспорт по типу: `stdio`, `http`, `sse`, `websocket`
- `get_server_tools()` — MCP `tools/list` с кэшем (TTL 30 мин)
- `call_tool()` — MCP `tools/call`, пробрасывает `WORKSPACE_FOLDER` в stdio env (из MCP roots)
- Опциональный Langfuse tracing

### `services/tool_extractor.py`

- Обходит все серверы из registry
- Для каждого: `tools/list` → `ToolMetadata` (tool_id, server_name, tool_name, description, text_content, schema)
- `text_content` = description + имена/описания параметров (для индексации)
- Retry (3×), таймаут на сервер (cap 60s для `index-tools`)

### `services/tool_indexer.py`

- **Полная пересборка** индекса при каждом запуске `index-tools`
- DROP vector index, fulltext index, все MCPTool nodes
- Embeddings через `EmbeddingGenerator` + провайдер (LM Studio / OpenRouter)
- FalkorDB graph: `Tools → MCPServerGroup → MCPServer → MCPTool`
- Индексы: `VECTOR INDEX ON MCPTool.embedding`, `FULLTEXT INDEX ON MCPTool.text_content`

### `services/tool_search.py` (`MCPToolSearch`)

- Гибрид: vector (вес 0.4) + fulltext BM25 (вес 0.6)
- Нормализация scores → combined_score
- Auto-detect server из query (`github`, `code-tasks`, `obsidian-docs`, …)
- Boost ×1.2 если server совпал с query
- Fallback в `mcp_server.py`: keyword search + intent mapping (`intent_mapping.py`)

### `transports/`

| Файл | Протокол |
|------|----------|
| `stdio.py` | subprocess, JSON-RPC newline-delimited; `session_mode=oneshot\|persistent` |
| `http.py` | REST MCP |
| `sse.py` | Server-Sent Events |
| `websocket.py` | ws:// / wss:// |

### `config/registry.json`

- `groups[]` — категории серверов (development, research, vcs, …)
- `mcpServers{}` — конфиг каждого сервера: command/args/env или url/headers
- `${VAR}` подстановка из `.env` и `os.environ` (`env_loader.py`)

### CLI: `code-index index-tools`

```
registry → ServerManager → ToolExtractor → MCPToolIndexer → FalkorDB
```

После индексации — `validate_step_tools` (проверка step-tools в memories DB, зависит от graphindex).

## Зависимости от graphindex (что мешает вынести как есть)

| Компонент | Зависимость |
|-----------|-------------|
| Индексация | `ConnectionManager`, FalkorDB adapter |
| Поиск | `VectorSearch`, `FullTextSearch` (Cypher к FalkorDB) |
| Embeddings | `EmbeddingGenerator`, `LMStudioProvider`, `OpenRouterProvider`, `Settings` |
| Мониторинг | `code_index.monitoring` (Langfuse) — опционально |
| Валидация | `validate_step_tools` — специфично для graphindex workflows |

## Поток поиска (runtime)

1. Клиент вызывает `search_tools(query="создать задачу")`
2. `MCPToolSearch.search()` — vector + fulltext в FalkorDB DB `tools`
3. Если пусто / ошибка → keyword fallback по registry + intent mapping
4. Клиент вызывает `call_server_tool(server_name, tool_name, arguments)`
5. `ServerManager` проксирует на нужный MCP-сервер

## Поток индексации (offline / startup)

1. Загрузить `registry.json`
2. Для каждого сервера: connect → `tools/list`
3. Собрать `text_content` для каждого tool
4. Batch embeddings через HTTP API (LM Studio / OpenRouter / любой OpenAI-compatible)
5. Записать в storage + создать vector + fulltext индексы

## Метрики (из README graphindex)

| Подход | Tools в контексте | Токены |
|--------|-------------------|--------|
| Все MCP напрямую | 50 | ~7 900 |
| Router (lazy) | 2 | ~262 |

## Вывод для standalone-проекта

**Переносить без изменений нельзя** — ядро router (transports, server_manager, mcp_server) относительно автономно (~2.5k LOC Python), но **поиск и индексация жёстко привязаны к FalkorDB**.

Для отдельного `mcp-router` нужно:

1. Вырезать router-модули из `code_index.mcp.router`
2. Заменить FalkorDB storage на компактное локальное хранилище
3. Убрать graphindex-специфику (`validate_step_tools`, memories DB)
4. Добавить startup reindex + docker-compose
5. Сохранить контракт: `search_tools` + `call_server_tool`, registry format совместим
