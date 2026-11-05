# mcp-router

[English](README.md)

MCP-прокси с ленивой загрузкой инструментов и гибридным поиском. Клиенты подключаются только по URL — локальная установка MCP на хосте агента не нужна.

## Для кого

- **Команды / ops**, у которых много MCP-серверов на общем хосте и нужен один стабильный URL для Cursor, Claude Desktop и других MCP-клиентов.
- **Разработчики агентов**, которым нужны десятки инструментов без копирования каждого сервера в конфиг каждого клиента.
- **Не замена** одному локальному MCP, который уже подключён напрямую — роутер имеет смысл, когда каталог растёт или секреты нельзя держать на ноутбуке.

## Что это

Один HTTP-эндпоинт, который:

- регистрирует множество downstream MCP-серверов (stdio / HTTP / SSE / WebSocket);
- индексирует их инструменты в SQLite;
- отдаёт компактный router-surface (`search_tools`, `call_server_tool`, …), чтобы агенты находили и вызывали инструменты без предварительной загрузки всех серверов.

## Зачем (против прямого подключения MCP в клиент)

| MCP напрямую в клиенте | Через mcp-router |
|------------------------|------------------|
| По одной записи на сервер; секреты и URL дублируются в каждом клиенте | Один URL; реестр и секреты на хосте роутера |
| Клиент заранее грузит / листит все инструменты (раздувает контекст) | Гибридный поиск (BM25 + векторы); вызывается только релевантное |
| Приватные / LAN-серверы требуют VPN или публичный доступ | Downstream остаётся в сети роутера; клиент видит только прокси |
| Сложно делить один набор инструментов между людьми и агентами | Общие compose + `.env` + registry; опционально Bearer и strict allow-list |

## Стек технологий

| Слой | Выбор |
|------|--------|
| HTTP API | FastAPI + Uvicorn |
| Протокол | MCP Streamable HTTP (`/mcp`, опционально strict `/mcp/v2`) |
| Индекс | SQLite + FTS5 (BM25) + sqlite-vec ANN (cosine fallback, если sqlite-vec недоступен) |
| Embeddings | Удалённый OpenAI-compatible API **или** локальный fastembed |
| Downstream-транспорты | stdio, HTTP, SSE, WebSocket |
| Деплой | Docker Compose; конфиг через `.env` + `config/registry.json` |

**Векторный поиск:** предпочитает sqlite-vec ANN. Если sqlite-vec отсутствует или падает, используется brute-force cosine по эмбеддингам с лимитом **5000** строк (`BRUTE_FORCE_VECTOR_SCAN_LIMIT`) и warning при большем индексе. Для больших индексов ставьте sqlite-vec.

## Быстрый старт (Docker)

Значения по умолчанию совпадают с `.env.example`: `HOST_PORT=8000`, контейнер слушает `MCP_ROUTER_PORT=8000`.

```bash
cp .env.example .env
cp config/registry.json.example config/registry.json
# Отредактируйте .env (HOST_PORT, embeddings, API keys) и config/registry.json
docker compose up --build -d
curl "http://localhost:${HOST_PORT:-8000}/health"
```

Дополнительно: `curl "http://localhost:${HOST_PORT:-8000}/status"`.

Опубликованный порт меняется через `HOST_PORT` в `.env` (маппинг `${HOST_PORT}:${MCP_ROUTER_PORT}` в `docker-compose.yml`). Секреты и URL downstream остаются в `.env` и подставляются в `config/registry.json` как `${VAR}`.

## Конфигурация

| Файл | Назначение |
|------|------------|
| `.env` | Порты, auth, CORS, embeddings, API keys (копия из `.env.example`) |
| `config/registry.json` | Downstream MCP-серверы (копия из `config/registry.json.example`; в gitignore) |
| `config/aliases.json` | Опциональные RU/EN-синонимы для буста поиска |

**Публичный SoT реестра:** `config/registry.json.example` (только плейсхолдеры `${VAR}`). Не коммитьте живой registry с приватными хостнеймами или секретами. Перед `visibility=public` на GitHub см. [docs/PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md).

### Основные переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `HOST_PORT` | `8000` | Порт на хосте, который публикует `docker compose` |
| `MCP_ROUTER_HOST` | `0.0.0.0` | Адрес bind внутри процесса/контейнера |
| `MCP_ROUTER_PORT` | `8000` | Порт прослушивания внутри процесса/контейнера |
| `REGISTRY_PATH` | `config/registry.json` | Путь к реестру MCP-серверов |
| `INDEX_DB_PATH` | `data/tools.db` | Путь к SQLite-индексу |
| `ALIASES_PATH` | `config/aliases.json` | RU/EN-синонимы серверов/инструментов |
| `SHADOW_RANKER` | `false` | Логировать shadow-ранжирование; агент видит baseline RRF |
| `ALIAS_BOOST` | `1.35` | Множитель score при совпадении с alias |
| `EMBEDDING_URL` | — | Вместе с `EMBEDDING_MODEL` — удалённый OpenAI-compatible API |
| `EMBEDDING_MODEL` | — | Модель эмбеддингов (нужна вместе с `EMBEDDING_URL`) |
| `LOCAL_EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Локальная CPU-модель, если URL не задан |
| `ROUTER_API_KEY` | — | Опциональный Bearer для `/mcp` и `/mcp/v2` |
| `ALLOWED_ORIGINS` | `*` | CORS origins через запятую |
| `STRICT_MCP_ENABLED` | `true` | Монтировать strict-surface на `/mcp/v2` |
| `STRICT_ALLOWLIST` | — | CSV / JSON / путь — курируемый набор для `/mcp/v2` |
| `STRICT_READ_ONLY` | `false` | На `/mcp/v2` только инструменты с `annotations.readOnlyHint=true` |
| `REINDEX_ON_START` | `true` | Реиндекс при старте |
| `REINDEX_BACKGROUND` | `true` | Неблокирующий реиндекс |
| `REGISTRY_HOT_RELOAD` | `true` | Поллинг `registry.json` без рестарта |
| `REGISTRY_POLL_INTERVAL_SEC` | `2.0` | Интервал hot-reload (секунды) |
| `HEALTH_PROBE_ENABLED` | `true` | Периодические `tools/list` к downstream |
| `HEALTH_QUARANTINE_ENABLED` | `true` | Помечать/пропускать упавшие серверы; блокировать их вызовы |

## Безопасность (публичный хост)

По умолчанию CORS = `ALLOWED_ORIGINS=*` и защита от DNS-rebinding выключена — нормально для доверенной LAN, **не** для хоста из публичного интернета.

Если mcp-router доступен снаружи доверенной сети:

1. Задайте `ROUTER_API_KEY` и требуйте `Authorization: Bearer …` у клиентов.
2. Задайте `ALLOWED_ORIGINS` явным allowlist через запятую (не оставляйте `*`).
3. Для недоверенных клиентов предпочитайте `/mcp/v2` с `STRICT_ALLOWLIST`, чтобы был доступен только курируемый набор инструментов.

## Конфиг Cursor

Используйте `localhost` (или свой hostname) и тот же порт, что `HOST_PORT` в `.env` (по умолчанию `8000`).

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

С Bearer-auth:

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <ROUTER_API_KEY>"
      }
    }
  }
}
```

Если `HOST_PORT=8010`, укажите `http://localhost:8010/mcp`.

## Инструменты роутера

| Инструмент | Описание |
|------------|----------|
| `get_router_status` | Здоровье, состояние индекса/реиндекса, режим embeddings, probes, reconnect hints |
| `search_servers` | Поиск зарегистрированных downstream-серверов (не инструментов); возвращает `search_id` |
| `search_tools` | Гибридный поиск с confidence; результаты группируются для удобства и ранжирования; возвращает `search_id` |
| `call_server_tool` | Прокси-вызов с нормализованным результатом и typed remediation; возвращает `call_id` |
| `review` | Оценка прошлого `call_id` или `search_id` (`useful` / `partly` / `not_useful` / `not_sure`) |
| `feedback` | Локальный отчёт quality/friction/other; опционально `call_ids` / `search_ids` |

### Feedback / review (только локально)

После `search_tools` / `call_server_tool` агенты могут оставить структурированные сигналы для ранкера:

- `review(usefulness=..., call_id=...)` или `review(..., search_id=...)` — ровно одна цель; опционально `reason` (≤200 символов). Идемпотентно на цель (`already_reviewed`).
- `feedback(category, message, call_ids?, search_ids?)` — категории: `quality`, `friction`, `other`. Отсутствие audit-ссылок не отклоняет запрос; `verified_*` показывает, что удалось связать.

Запись идёт в SQLite (`call_audit`, `reviews`, `feedback`) с join к `search_log`. **Без вызова downstream MCP.** В query/reason редактируются секреты вида bearer/API-key.

### Typed remediation errors

При ошибке `call_server_tool` (или если поиск невозможен / индекс деградирован) JSON содержит actionable-поля **без секретов и сырых заголовков**:

| Поле | Смысл |
|------|-------|
| `failure_kind` | Одно из: `downstream_unreachable`, `auth_failed`, `index_degraded`, `tool_not_found`, `timeout` |
| `server_name` | Downstream-сервер (если известен) |
| `reconnect_hint` | Что не так / куда смотреть |
| `next_action` | Конкретный следующий шаг для агента (также как `retry_hint`) |
| `error_source` | Legacy-бакет: `router` / `downstream` / `tool` |

`search_tools` выставляет те же remediation-поля, когда индекс pending / indexing / error или пуст (`tool_count == 0`). Успешный поиск остаётся `ok: true` и добавляет hints только при деградации.

Учётные данные остаются на хосте роутера (`.env` / заголовки registry) — в тексте ошибок редактируются `Bearer`, `api_key` и похожие токены.

## Формат registry

Серверный `config/registry.json`: на верхнем уровне `mcpServers` — имена серверов → конфиги транспортов. Опциональные `groups` только группируют серверы для удобства и ранжирования — это не отдельный продукт. Downstream-серверы должны быть достижимы с хоста/контейнера роутера.

## CLI

```bash
pip install -e .
mcp-router serve              # HTTP-сервер на :8000
mcp-router index-tools        # Пересобрать SQLite-индекс
```

## Разработка

После клонирования один раз:

```bash
./scripts/setup-dev.sh
```

Создаёт `.venv`, ставит `pip install -e ".[dev]"` и **`pre-commit install`** — без этого git-хук не появится. Конфиг `.pre-commit-config.yaml` сам по себе хуки не устанавливает.

Или вручную:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install --install-hooks
```

Проверки:

```bash
make check              # pre-commit (lint, typecheck, import-linter, tests)
make lint-imports       # контракты слоёв import-linter
make context-drift      # изменённые sources → docs/context
make test               # pytest
ruff check src tests    # lint
ruff format src tests   # format
mypy                    # typecheck
```

Pydantic-модели — в `src/mcp_router/models/` (`Registry`, `Settings`, `ToolMetadata`, …). JSON registry валидируется при загрузке через `services/registry_loader.py`.

## Эндпоинты

| Путь | Описание |
|------|----------|
| `GET /health` | Liveness |
| `GET /status` | tool_count, last_indexed_at, embedding status, search_quality, downstream_health probes, mcp_surfaces |
| `POST /mcp` | MCP Streamable HTTP — **полный** surface |
| `POST /mcp/v2` | MCP Streamable HTTP — **strict** surface (allow-list / опционально read-only) |
| `POST /admin/reindex` | Фоновый реиндекс |

### Когда использовать `/mcp/v2`

Направляйте безопасных / недоверенных клиентов на `/mcp/v2`, если им нужен только курируемый набор серверов/инструментов. Агентам с полным реестром оставляйте `/mcp`.

Allow-list задаётся через env (приоритетнее) или `strictProfile` в `registry.json`:

```bash
# CSV: целые серверы и/или ключи server:tool
STRICT_ALLOWLIST=docs,web-search,wiki:lookup

# или JSON
STRICT_ALLOWLIST='{"servers":["docs","web-search"],"tools":[],"read_only":true}'
```

```json
{
  "strictProfile": {
    "servers": ["docs", "web-search"],
    "tools": ["wiki:lookup"],
    "readOnly": false
  },
  "mcpServers": { }
}
```

Вызовы вне allow-list возвращают typed `failure_kind=strict_denied`. Пустой allow-list на `/mcp/v2` запрещает все `call_server_tool`, пока не настроен.

## Troubleshooting

- **Пустой поиск** — смотрите `/status`: дождитесь `index_status: ready`, проверьте `EMBEDDING_URL` или keyword fallback.
- **Падает downstream** — сервер должен быть в registry, переменные `${VAR}` заданы в `.env`.
- **401 Unauthorized** — в Cursor нужен тот же `Authorization: Bearer`.
- **stdio в Docker** — npx-серверам нужен Node в образе (есть в Dockerfile).
- **`entrypoint.sh: no such file or directory` (Windows)** — CRLF в shell-скриптах. Обновите репозиторий (`.gitattributes` + Dockerfile) и пересоберите: `docker compose build --no-cache && docker compose up -d`.
- **Ошибки embedding / reindex** — при `EMBEDDING_URL` + `EMBEDDING_MODEL` используется remote API, иначе local CPU (fastembed). Режим смотрите в `get_router_status`.
- **Cursor `fetch failed` / ECONNREFUSED** — контейнер запущен (`docker ps`), порт совпадает с `HOST_PORT` в `.env` (по умолчанию `8000` → `http://localhost:8000/mcp`), `/status` показывает `index_status: ready`.

## Доп. документы

Операционные заметки (часть файлов в `docs/` историческая и может отставать от кода):

- [docs/PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md) — перед GitHub `visibility=public`
