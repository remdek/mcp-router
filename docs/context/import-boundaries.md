---
title: Enforced import boundaries
status: shipped
sources:
  - pyproject.toml
  - .github/workflows/ci.yml
  - Makefile
  - src/mcp_router/services/__init__.py
  - src/mcp_router/index/__init__.py
  - src/mcp_router/storage/__init__.py
  - src/mcp_router/transports/__init__.py
  - src/mcp_router/models/__init__.py
  - src/mcp_router/embedding/__init__.py
related:
  - proxy-call.md
  - search.md
---

# Import boundaries

[import-linter](https://import-linter.readthedocs.io/) contracts live under
`[tool.importlinter]` in `pyproject.toml`. CI and `make lint-imports` run
`lint-imports` so cycles and upward imports fail the build.

## Intended layers

```text
http_server / http_middleware / http_mcp_tools / cli   # adapters
        ↓
services / index           # application
        ↓
storage / transports / embedding / models   # leaves
```

Adapters may import everything below. Leaves must not import adapters or
siblings that sit above them.

## Active contracts

| Contract | Rule |
|---|---|
| Services ↔ adapters | `services` must not import `http_server`, `http_middleware`, `http_mcp_tools`, or `cli` |
| Index ↔ adapters | `index` must not import `http_server`, `http_middleware`, `http_mcp_tools`, or `cli` |
| Storage leaf | `storage` must not import adapters, services, index, transports, embedding |
| Transports leaf | `transports` must not import adapters, services, index, storage, embedding |
| Models leaf | `models` must not import any other `mcp_router` package |
| Embedding | `embedding` must not import adapters, services, index, storage, transports |

No large layer refactor is required — these contracts pin the current
directionality so HTTP/CLI stay composition roots as the router grows.
