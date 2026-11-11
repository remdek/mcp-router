# ADR 0002: Invoke-time policy (deny / allow / denyDestructive)

| Field | Value |
|-------|-------|
| Status | Accepted |
| Date | 2026-11-11 |
| Issue | WOR-1612 (parent WOR-1610 / ToolLens comparison) |
| Decides | Permission model for `call_server_tool`; relationship to discovery and `/mcp/v2` |

## Context

[ToolLens](https://github.com/krishnashakula/toollens) separates **discovery** from
**permission**: finding a tool never grants the right to run it. Their gateway
checks policy on every `invoke_tool` with order:

1. deny globs (win over everything)
2. `denyDestructive` (unless an allow glob names the tool)
3. allow globs
4. configured `default` (ToolLens fails closed when policy is the built-in default)

mcp-router already has a **strict surface** (`/mcp/v2`) with an exact allow-list
and optional `readOnlyHint` gate. The default surface (`/mcp`) is open for the
full registry — appropriate for a trusted LAN / shared team proxy.

WOR-1610 recommended borrowing ToolLens invoke policy without replacing the
strict surface or changing the thin tool surface (ADR 0001).

## Decision

### 1. Discovery ≠ permission

| Path | Policy role |
|------|-------------|
| `search_tools` / `search_servers` | **No** invoke-policy filter (identity + schema discovery stay searchable) |
| `get_tool_schema` | Returns schema **and** a `permission` verdict (informational; does not block) |
| `call_server_tool` | **Enforces** invoke policy when configured; denials → `failure_kind=policy_denied` |

### 2. Evaluation order (when policy is active)

Same as ToolLens, matched against tool ids `server:tool` (also accept `server/tool`
in globs for ToolLens-shaped configs):

1. **deny** globs → deny
2. **denyDestructive** if `annotations.destructiveHint === true` and no allow match → deny
3. **allow** globs → allow
4. **default** (`allow` \| `deny`)

**Caveat:** tools without `destructiveHint` are **not** treated as destructive.
Annotations are untrusted hints and may only tighten policy (never open a deny).

### 3. Default when no policy is configured: keep `/mcp` open

| Situation | Behavior |
|-----------|----------|
| No `INVOKE_POLICY` and no registry `invokePolicy` | Policy **inactive** — current open `/mcp` call behavior preserved |
| Explicit config present | Policy **active**; omitted `default` in JSON → **`deny`** (fail-closed once opted in) |

**Rationale:** mcp-router is a LAN/shared HTTP proxy with an existing open default
surface and a separate curated `/mcp/v2`. ToolLens is a local stdio lens that
owns the client's entire tool surface — fail-closed-by-default fits that product.
Flipping our unset default to deny would break every current deployment.
Operators who want ToolLens-style posture set e.g.
`{"default":"deny","allow":["hindsight:*"],"denyDestructive":true}`.

### 4. Coexistence with `/mcp/v2` strict allow-list

Both layers apply independently; **both must allow** the call:

1. Invoke policy (both `/mcp` and `/mcp/v2` when active)
2. Strict allow-list / read-only (only when surface is `strict`)

`search_tools` on `/mcp/v2` still filters by the strict allow-list (unchanged).
Invoke policy still does **not** hide tools from search.

### 5. Configuration

- Env: `INVOKE_POLICY` — JSON object, `@path`, or `.json` path (wins over registry)
- Registry: `invokePolicy` / `invoke_policy` with keys `default`, `allow`, `deny`,
  `denyDestructive` (unknown keys rejected)

Example:

```json
{
  "default": "allow",
  "deny": ["playwright:*", "github:delete_*"],
  "allow": ["deploy:restart"],
  "denyDestructive": true
}
```

## Consequences

- New module `services/invoke_policy.py`; wired in `AppState`, lifespan, hot-reload.
- New remediation kind `policy_denied` (distinct from `strict_denied`).
- Docs: README / SKILL / `docs/context/proxy-call.md`; status exposes
  `mcp_surfaces.invoke_policy`.
- Does not enlarge the agent tool surface (ADR 0001 Layer A unchanged).

## References

- ToolLens `src/policy.rs`
- `docs/adr/0001-context-budget-invariants.md`
- `services/strict_policy.py` — `/mcp/v2` allow-list (orthogonal)
