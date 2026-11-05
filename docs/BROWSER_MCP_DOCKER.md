# Browser MCP servers in Docker

## TL;DR

| Problem | Fixable with apt packages? | Notes |
|---------|--------------------------|-------|
| `lsof: not found` | **Yes** — `procps` + `findutils` in Dockerfile | Cosmetic startup noise; server continues |
| `xargs: kill: No such file` | **Yes** — same packages | |
| Browser automation (`tools/call`) | **Partially** | Needs Chrome extension on host + port `9009` published |
| Headless browser inside container | **Different server** | Use `chrome-devtools-mcp`, not `@browsermcp/mcp` |

`lsof` is **not** the root cause. BrowserMCP is architecturally a **bridge** between MCP (stdio) and a **Chrome extension** in the user's desktop browser.

## How @browsermcp/mcp works

```
Cursor / mcp-router  --stdio-->  npx @browsermcp/mcp
                                        |
                                        v
                              WebSocket server :9009
                                        ^
                                        |
                         Chrome extension (user clicks "Connect")
                                        |
                                   Chrome tab
```

Source (`@browsermcp/mcp@0.1.3`):

1. On start, runs `lsof -ti:9009 | xargs kill -9` to free port 9009 (fails if `lsof` missing — **logged, not fatal**).
2. Starts `WebSocketServer` on port **9009**.
3. `tools/list` — returns static tool schemas (works **without** extension).
4. `tools/call` — sends commands over WebSocket to the extension; **requires** user to connect a tab.

Official docs: [setup extension](https://docs.browsermcp.io/setup-extension), [troubleshooting](https://docs.browsermcp.io/troubleshooting).

## What we fixed in the Docker image

```dockerfile
procps      # provides lsof
findutils   # provides xargs
```

`docker-compose.yml` publishes `9009:9009` so the host Chrome extension can reach the in-container WebSocket server.

### Indexing via mcp-router

For `tools/list` during reindex, extension is **not** required. After installing `procps`, browsermcp should index normally.

Set a short timeout if needed:

```json
"browsermcp": {
  "command": "npx",
  "args": ["-y", "@browsermcp/mcp@latest"],
  "timeout": 15
}
```

### Runtime proxy (`call_server_tool`)

1. Keep browsermcp in registry (`index: true` or `false` — your choice).
2. `docker compose up` with port `9009:9009`.
3. Install [Browser MCP extension](https://browsermcp.io/install) in Chrome on the **host**.
4. Open a tab → extension → **Connect** (connects to `localhost:9009` → container).
5. Call tools through mcp-router.

**Limitation (oneshot stdio):** each `stdio` spawn starts a new npx process and WS server. Only one browsermcp instance should own port 9009 at a time. Heavy parallel calls may conflict.

For **playwright** / **chrome-devtools**, mcp-router uses `session_mode: persistent` (WOR-874): one keep-alive stdio process per server so navigate → snapshot → click share the same Chromium. Idle TTL (`idle_timeout_sec`, default 300s) and `cleanup_all` on shutdown close the process. Light stdio servers (github, searxng, …) stay `oneshot`. Revert: set `"session_mode": "oneshot"` on the registry entry.

## What cannot be solved with apt

- **No Chrome inside slim image** — BrowserMCP does not launch Chromium; it controls the host browser via extension.
- **Extension on Windows cannot reach container :9009** without port publishing (now in compose).
- **Fully headless in container** — use a different MCP server.

## Docker-native alternative: chrome-devtools-mcp

For automation entirely inside the container (no host extension). Official package: [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp).

When the image is built with `PLAYWRIGHT_INSTALL=true`, Chromium lives under `/ms-playwright/chromium-*/chrome-linux64/chrome`. The Dockerfile creates a stable symlink `/ms-playwright/chromium` → the latest `chromium-<rev>` folder — **always** point registry paths at the symlink, not a pinned revision.

### Playwright MCP (stdio)

Prefer the **global** binary installed at image build (`npm install -g @playwright/mcp`) over `npx -y @playwright/mcp@latest`. Concurrent probes/reindex can corrupt the npx cache (`Cannot find module …/playwright-core/…`) and the process exits before answering → `MCP stdio server closed stdout before responding`.

```json
"playwright": {
  "command": "playwright-mcp",
  "args": [
    "--headless",
    "--no-sandbox",
    "--browser",
    "chromium",
    "--executable-path",
    "/ms-playwright/chromium/chrome-linux64/chrome"
  ],
  "timeout": 180
}
```

```json
"chrome-devtools": {
  "command": "npx",
  "args": [
    "-y",
    "chrome-devtools-mcp@latest",
    "--headless",
    "--isolated",
    "--no-usage-statistics",
    "--executablePath",
    "/ms-playwright/chromium/chrome-linux64/chrome",
    "--chromeArg=--no-sandbox",
    "--chromeArg=--disable-dev-shm-usage"
  ],
  "timeout": 180,
  "description": "Chrome DevTools MCP: inspect/debug pages via CDP — performance traces, network, console, DOM snapshots, screenshots (chrome-devtools, CDP, LCP, INP, CLS)",
  "groups": ["automation", "development"]
}
```

### After rebuild / registry edit

1. Confirm symlink: `docker exec mcp-router ls -la /ms-playwright/chromium/chrome-linux64/chrome`
2. Smoke stdio: `docker exec mcp-router playwright-mcp --help`
3. Edit live `registry.json` on the router host → `docker compose restart` so `ServerManager` reloads
4. `POST /admin/reindex` (or rely on `REINDEX_ON_START`)
5. Accept: `curl -s http://<ROUTER_HOST>:8010/status | jq '.downstream_health | {healthy_servers, failed_servers}'` — `playwright` in healthy, not failed

## Registry flags (mcp-router)

| Flag | Effect |
|------|--------|
| `"disabled": true` | Server ignored completely |
| `"index": false` | Registered and callable, skipped during reindex |
| `"timeout": 15` | Per-server index/call timeout (seconds) |
| `"session_mode": "persistent"` | Keep one stdio process across `tools/call` (default for `playwright`, `chrome-devtools`) |
| `"session_mode": "oneshot"` | Spawn → one request → exit (default for other stdio) |
| `"idle_timeout_sec": 300` | Close idle persistent process (persistent only) |

## Recommendation

| Deployment | browsermcp | Alternative |
|------------|------------|-------------|
| Docker on server (no desktop) | `disabled: true` | `chrome-devtools-mcp` + Chromium in image |
| Docker on dev laptop | OK with `9009:9009` + extension | |
| Cursor stdio (no router) | Native setup per browsermcp docs | |

## Agent MCP config: one Chromium only (WOR-875)

When the agent Cursor/MCP config already mounts **mcp-router** (e.g. `http://<ROUTER_HOST>:8010/mcp`), **do not** also register a standalone `playwright` entry that runs `npx @playwright/mcp@latest`.

That second process is a **different Chromium**. Mixing paths causes the classic blank:

| Path | After `browser_navigate` → example.com |
|------|----------------------------------------|
| `mcp-router` → `call_server_tool` → `playwright` | `https://example.com/` |
| Native Cursor namespace `playwright` (local npx) | `about:blank` |

**Rule:** with mcp-router present, browser only via router (`call_server_tool` / router-exposed tools). Prefer `browser_evaluate` / `browser_snapshot` over `browser_run_code_unsafe` for page reads.

**Config:** remove the local `playwright` block from `~/.config/mcp/mcp.json` and `~/.cursor/mcp.json` (and any Multica agent MCP overlay) whenever `mcp-router` is listed. Fallback without router reachability: restore a single direct `playwright` entry — never both.

**Optional:** if a runtime must keep a direct `playwright` key, point it at the **same** persistent server (HTTP/SSE through router or shared CDP). Do not spawn a second `@playwright/mcp` via npx alongside the router’s persistent session.
