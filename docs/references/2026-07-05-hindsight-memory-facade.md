# Hindsight memory facade (admin-authed harness proxy + boot provisioning)

**Date:** 2026-07-05
**Status:** Shipped (branch `feat/hindsight-memory-facade`)
**Scope:** `src/ach_agent/memory/{hindsight.py,facade.py}`, `src/ach_agent/config/schema.py`,
`src/ach_agent/main.py`. Cross-repo follow-up in `ach-runtime` (see below).

## Problem

The prior wiring pointed opencode's `memory-0` MCP server **directly at Hindsight**. That
exposed the agent to Hindsight's full MCP surface — ~30 tools including destructive ones
(`hindsight_delete_bank`, `hindsight_delete_memory`, `hindsight_clear_memories`) and every
admin/bank-management tool. Two further defects:

1. **The harness never provisioned anything.** `mentalModels` was a `list[str]` of ids the
   agent was told to read, but nothing ever ran `create_bank` / `create_mental_model`, so those
   models did not exist — the reads returned nothing. Provisioning is an admin responsibility
   the agent must not (and could not, without admin auth) perform.
2. **Wrong tool names.** The read path called `memory_get_mental_model`; the live deployment's
   tools are namespaced `hindsight_*` (`hindsight_get_mental_model`), so the call 404'd silently
   and was swallowed by the fail-open path — memory looked "degraded" for a fixable reason.

## Decision

The **harness** owns the Hindsight relationship with an **admin secret** (Bearer), never the
`ek_`. Two harness-side responsibilities:

1. **Boot provisioning** (`provision_memory`, boot-once, idempotent, fail-open): `create_bank`
   → `create_mental_model` for each spec → `refresh_mental_model` for the `autoRefresh` ones.
2. **An in-process MCP facade** (`MemoryFacade`, FastMCP on `127.0.0.1`) exposing **exactly
   four** agent-facing tools: `memory_recall`, `memory_reflect`, `memory_get_mental_model`,
   `memory_retain`. Each injects the harness-owned `bank_id` + the admin Bearer, then maps to
   the real `hindsight_*` tool. opencode's `memory-0` points at the facade URL, **not** at
   Hindsight.

The agent never sees `bank_id`, the admin secret, or any admin/destructive tool.

### The single seam

Every harness→Hindsight call routes through one function, `call_hindsight(endpoint, secret,
tool, args)`, so tests monkeypatch a single point and auth/timeout policy lives in one place.

### SDK note (assumed-Bearer, and a header-injection wrinkle)

Auth, when present, is assumed **Bearer**: `Authorization: Bearer <secret>`, isolated to
`hindsight_auth_headers`. The installed `mcp` SDK's `streamable_http_client(url, *,
http_client=None, terminate_on_close=True)` has **no `headers=` kwarg** (an older API the plan
assumed) — headers are injected by pre-building the httpx client via
`create_mcp_http_client(headers=...)` (which also applies the SDK's recommended MCP timeouts)
and passing it as `http_client=`. The harness owns that client's lifecycle (`async with`),
since the transport only closes clients it created.

### Auth is OPTIONAL; fail-open is total

- `auth` **absent** → run unauthenticated (Hindsight on an internal/cluster no-auth URL). OK.
- `auth` **present but its env var unset** → misconfig → **fail-open degrade** (do NOT silently
  drop the intended auth; skip provisioning, don't start the facade, run without memory).
- `auth` **resolved** → Bearer.

Every memory error (unreachable, unset secret, bad response, local bind failure of the facade
is the one exception — that is a genuine boot error, same as the sibling localhost proxies)
degrades to "run without memory" and increments `MEMORY_DEGRADED`; it never raises into a turn
or crashes boot.

## Config shape (CONTRACT §2, breaking change)

```jsonc
"memory": {
  "type": "hindsight",
  "hindsight": {
    "endpoint": "https://hindsight.../mcp",
    "bank": "gitlab-pr-review",                 // static, harness-owned; agent NEVER sees/sets it
    "auth": { "env": "ACH_SECRET_MEMORY_HINDSIGHT" }, // OPTIONAL (omit for internal/no-auth URL). Bearer, NOT the ek_. env-only
    "mission": "AI code reviewer",              // optional; passed to create_bank
    "mentalModels": [                            // rich specs the harness provisions at boot
      { "id": "architecture", "name": "Architecture", "sourceQuery": "What is the architecture?",
        "autoRefresh": true, "maxTokens": 2048 }
    ]
  }
}
```

`mentalModels` changed from `list[str]` to `list[MentalModelSpec]` — **breaking**. The admin
secret env NAME joins the same `forwardEnv`-strip + log-redaction path as webhook/a2a secrets
(`collect_secret_env_names`), so it can never reach opencode's env or the logs.

## Per-repo partitioning: tags, not bank

`bank` is **static** and harness-owned. To scope memories per repo, tag them
(`memory_retain(content, tags=["repo:<name>"])`) and filter on recall — never template `bank`
from the inbound payload (T-04-03). The facade captures `bank` once at boot, so a templated
bank would not even reach the agent's recall/retain path.

## Follow-ups

- **`ach-runtime` (the Go operator) — separate PR, NOT in this change.** The CRD→contract
  render for `memory.hindsight` must emit the rich `mentalModels` objects, the `auth` env
  NAME, and `mission`. Until that lands, this schema is only usable via hand-authored local
  configs.
- ~~**Residual bank-templating in `engine_runner`.**~~ RESOLVED (2026-07-05, follow-up commit):
  `bank` templating is now rejected at config load (`HindsightParams._bank_static`, T-04-03) and
  the per-event `effective_bank` path was removed — the fetch, the facade, and the prompt's
  `{{ memory.bank }}` all use the one static bank. Per-repo partitioning is via tags, not bank.
- **Boot `list_tools` probe.** The `HINDSIGHT_*` names are module constants verified against the
  live deployment; a boot probe logging the actual tool names would catch a future rename
  loudly rather than via a swallowed 404.

## Related

[[ach-agent-hindsight-memory-facade]] (locked design note), [[ach-agent-memory-bank-tags-design]]
(tags-vs-bank), [[ach-agent-codemem-backend]] (the sibling `memory.type`).
