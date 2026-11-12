# ADR 0001: Context budget invariants

| Field | Value |
|-------|-------|
| Status | Accepted |
| Date | 2026-11-06 |
| Issue | WOR-1536 (parent research WOR-1535 / pi-mcp-adapter) |
| Decides | What may enter the agent hot path by default; what needs measurement first |

## Context

mcp-router exists so clients do **not** load hundreds of downstream tool
definitions into the model. The product thesis is:

1. **Context is the budget** — every field returned on the default search→call
   path competes with the user's task for tokens.
2. **Compose before inventing** — prefer thinning / gating / truncating the
   existing surface over new host-coupled modes (deferred tool names, OAuth UX,
   in-process plugins).

WOR-1535 compared [pi-mcp-adapter](https://github.com/nicobailon/pi-mcp-adapter)
and listed patterns worth adopting (schema-on-demand, output guard, agent-visible
failure backoff). This ADR freezes the **invariants** before those spikes so
P0/P1 work cannot silently inflate the hot path.

## Decision

### Hot path (default agent turn)

An agent turn that uses the router typically pays for three layers of tokens.
Anything added to these layers without measurement is a regression unless it
clearly *reduces* total tokens for the same recall quality.

| Layer | What the model sees today | Budget rule |
|-------|---------------------------|-------------|
| **A. Tool surface** | Fixed small set: `search_tools`, `search_servers`, `call_server_tool`, `get_tool_schema`, `get_router_status`, `feedback`, `review` (+ their input schemas) | Keep thin. `get_tool_schema` is the schema-on-demand second lookup (WOR-1537) — do not promote downstream tools into this surface. Do not add deferred/host-native names (`mcp__…`) into the model context. |
| **B. Search hits** | Per hit: `tool_id`, `server_name`, `tool_name`, `description`, `score`; **full `tool_schema` only on strong matches** (`schema_omitted` otherwise); plus envelope `ok`, `query`, `search_id`, `confidence`, `confidence_hint`, compact `router` status | Schema-on-demand shipped (WOR-1537): floor = `top_score * SEARCH_STRONG_MATCH_RATIO` (default 0.85; always include top-1). Weak hits are identity + description; fetch schema via `include_schema_for` / `get_tool_schema`. Default `limit` and `max_per_server` stay bounded. |
| **C. Call result** | Faithful relay of downstream `tools/call` result (normalized JSON) + `call_id` / typed remediation on failure; **huge bodies truncated** with `output_guard` + `full_output_path` under `data/call_artifacts/` | **Shipped (WOR-1538).** Defaults 50 KiB / 2000 lines (`OUTPUT_GUARD_*`). Do not invent a second API. Optional field `extract` is P1+, not default. |

Telemetry (`search_id` / `call_id`), quarantine, and remediation fields
(`failure_kind`, `next_action`, `reconnect_hint`) are **allowed** on the hot
path when they are short and actionable — they replace retries and guesswork.

### Invariants

1. **Discover → call.** Agents must search (or otherwise verify) before calling;
   the router must not dump the catalog into context to “help.”
2. **Faithful relay.** Arguments and validation stay downstream; the proxy does
   not re-model APIs to “save” tokens by inventing a parallel schema language.
3. **Measure before fattening.** Any change that adds fields, raises default
   `limit`, embeds full schemas for more hits, or returns unbounded call bodies
   on the default path needs a before/after token (or size) estimate — use
   `mcp-router tax` / `GET /admin/tax` (WOR-1613) and existing `review` /
   `feedback` / search telemetry where possible.
4. **Compose first.** Prefer gating (schema only on strong hits), truncation
   (output guard), and short failure reasons over new exposure modes or host
   classifiers.
5. **Ops stay off the model path.** Index rebuild, pool lifecycle, embedding
   provider choice, and registry secrets must not leak into tool results beyond
   the compact status already returned.

### Target directions (from WOR-1535 — not implemented by this ADR)

| Priority | Pattern | Intent for the budget |
|----------|---------|------------------------|
| P0 | **Schema-on-demand** (+ strong-match floor) | **Shipped (WOR-1537)** — omit `tool_schema` on weak search hits |
| P0 | **Output guard** | **Shipped (WOR-1538)** — truncate large call results; point to full artifact/path |
| P1 | **Failure backoff with reason** | **Shipped (WOR-1539)** — short “failed Ns ago: reason” beside quarantine |
| P1 | Optional **`extract`** on call | Return selected fields; full body in side channel |
| P2 | Lifecycle modes as product contract | Ops clarity; must not enlarge tool surface |

### Anti-goals

Do **not** pull into mcp-router (or the default agent context):

- In-process embedding of the router into the agent host (we stay a URL gateway)
- OAuth / keychain / setup panels / MCP UI / elicitation (host UX product)
- Vendor executor special-cases or plugin marketplaces
- Semantic search via a host-side classifier that duplicates our SQLite index
- Deferred tool names in the model context that bypass the thin surface

## Consequences

- Spikes for P0/P1 treat this document as the acceptance lens: success is fewer
  tokens on a typical search→call turn at equal recall quality.
- `docs/ARCHITECTURE.md` links here; runtime fragments under `docs/context/`
  stay the shipped how-it-works SoT and should note budget-impacting changes.
- PR authors use the checklist below.

## PR checklist — hot path tokens

Copy into the PR body (or confirm in review) when touching search, call, MCP
tool handlers, or result shaping:

- [ ] Does this increase tokens on the **default** search→call path?
- [ ] If yes: what was measured (hit count × schema size, result bytes, or
      equivalent), and why is the increase required?
- [ ] Could the same goal be met by **compose** (gate / truncate / omit) instead
      of adding surface or always-on fields?
- [ ] Are new fields short and actionable (or opt-in), not full dumps?
- [ ] Still no catalog dump / deferred tool promotion into the model context?
- [ ] Telemetry / remediation still do not leak secrets or raw transport headers?

## References

- `docs/ARCHITECTURE.md` — live package overview
- `docs/context/search.md`, `docs/context/proxy-call.md` — shipped pipelines
- `SKILL.md` — agent-facing thin-surface loop
- WOR-1535 research comment (pi-mcp-adapter pattern comparison)
