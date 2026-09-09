# ach-memory as an ach-agent memory backend

**Status:** spec, settled. Auth decided 2026-09-09 (§9). Ready to become an implementation plan.
**Supersedes for new agents:** `2026-07-05-hindsight-memory-facade.md` (the direct-Hindsight backend).
**Companion:** `ach-memory/docs/plans/2026-09-09-builtin-only-standing-context.md` (Plan A). Independent — this work runs against ach-memory as it stands today and only *benefits* from Plan A's `load_context` scope filter.

---

## 1. Goal

Replace the direct-Hindsight memory backend with ach-memory. The agent stops carrying a bank id, an admin secret, and a list of mental models; it gains per-project memory scoped by authorization rather than by a tag convention the prompt merely asks for.

## 2. Why

Measured on `gitlab-reviewer` (2026-09-08). `fetch_mental_model_summaries` calls Hindsight's `get_mental_model` once per configured model, per invocation, and injects the result into the system prompt. Hindsight defaults `detail="full"`, which returns the whole row — including `reflect_response.based_on`, the full text of every fact the synthesis cited, plus the refresh trace — pretty-printed with `json.dumps(indent=2)`. `maxTokens` bounds only the `content` field; the envelope around it is unbounded.

`hindsight.py:201` now passes `detail="content"`, which caps the immediate bleeding. The structural problems remain:

1. **One bank for every repository.** `bank: gitlab-ackstorm` is static and shared. `MentalModelSpec` has no `tags` field, so "Architecture Overview" synthesizes across every ACKstorm repo at once. The system prompt asks the model to tag memories `repo:<project-path>`; nothing enforces it and nothing reads it back.
2. **No budget.** Four models at 4096/2048/2048/2048 = 10 240 tokens of *content*, injected on every event including the Trivial tier the prompt's own decision table says should load nothing.
3. **A bank-wide admin secret** lives in the agent's environment.

ach-memory answers the first two outright: `load_context` is deadline-bounded and token-budgeted and returns only `content`, and project scope is resolved server-side through authz. The third it narrows rather than removes — see §9.

## 3. What ach-memory gives us

| Need | Mechanism |
|---|---|
| Bounded standing context | `load_context` — 2s deadline (`DEADLINE_SECONDS`), per-section `max_tokens`, a 5120-token global ceiling (`assemble_context`), `_model_text` takes only `content`, machine-readable `omissions`/`overages` for anything dropped or over budget |
| Hard ceiling | `USER_ALWAYS_IN_CONTEXT_BUDGET` / `PROJECT_ALWAYS_IN_CONTEXT_BUDGET` = 2560 each, enforced at registration |
| Per-repo isolation | project scope; `project_slug` resolved through `projects.resolve` + authz |
| Provisioning | `POST /v1/bootstrap {project_slug}` — idempotent; creates project, bank, the `project-context` built-in, and the exact-retain strategy |
| Standing models | built-ins `user-context` / `project-context` (2048 each). After Plan A, custom models can never be standing |
| Response compaction | `mcp/compact.py` strips fields no caller of this surface can use |

**The four configured mental models go away entirely.** `project-context`'s own source query already covers them: *"accepted decisions and useful rationale, constraints, conventions, non-obvious facts and history, and verified gotchas… distinguish rejected or superseded alternatives from active decisions."* That is `architecture` + `conventions` + `recurring-issues` in one, at 2048 tokens, on a delta refresh with a 300s floor. `team-reviewer` does not fit the remaining 512-token headroom and should not: reviewer/ownership context is a `recall` at review time, not standing context on every webhook.

## 4. Prior art to reuse, not reinvent

- **`ach-memory/src/memory/mcp/proxy.py`** — the client-side bridge. It already resolves a project (`MEMORY_PROJECT` env, else git origin), calls `POST /v1/bootstrap` once at startup, fills project-scoped arguments the model omitted, and forwards the rest unchanged. Its `bootstrap()` (`proxy.py:83`) is the exact call this harness needs, including its fail-open contract: *"a broken or slow service must cost this session its bootstrap, never its startup."* It is a **stdio** bridge started once, so it cannot serve a per-event project — but its behaviour is the reference.
- **`RepoCheckoutFacade`** (`main.py:496-509`) — a second loopback facade in the same boot block, and the clearest example of the invariant this one must also hold: the agent never sees the credential or the real endpoint, only the loopback URL.
- **`memory.codemem.project` templating** (`engine_runner.py:174-186`) — the existing precedent for a per-event, `{{ }}`-templated memory project, rendered from the same `ctx` as `channel.prompt`.
- **`MemoryFacade`** (`memory/facade.py`) — the loopback FastMCP shape, `LocalMcpHost`, and the fail-soft `_invoke`.

## 5. Configuration

```yaml
memory:
  type: ach-memory
  achMemory:
    endpoint: http://ach-memory.ach.svc:8000
    auth:
      secretKeyRef:
        name: ach-memory-key
        key: ACH_MEMORY_API_KEY
    project: "{{ payload.project.path_with_namespace }}"
    scope: project                       # user | project | both (default both)
    tools:                               # optional; omitted → the default set below
      - recall
      - reflect
      - retain
      - get_mental_model
      - list_mental_models
```

Gone from the CRD: `bank`, `mission`, `mentalModels`. `MentalModelSpec` is deleted outright. `auth` survives, but as a *user* key rather than a bank-wide admin secret (§9).

**`tools` — optional with a safe default.** Omitted, the facade exposes exactly `recall`, `reflect`, `retain`, `get_mental_model`, `list_mental_models`. Everything else ach-memory advertises — `create/update/delete_mental_model`, `forget/correct/restore`, the document and operation tools, working state, `sync_retain` — must be named explicitly. Validate each entry against the server's advertised set at boot and refuse to start on an unknown name, the same discipline the JWT loader uses (`ErrEmptyKid`): a typo'd tool name that silently does nothing is worse than a failed start.

**`project` is templated, `scope` is not.** `project` uses the same `{{ }}` engine and namespaces as `channel.prompt` — this is `memory.codemem.project`'s rule, not `memory.hindsight.bank`'s. The bank rule (`_bank_static`) existed because a templated *bank id* selects another tenant's memory directly. A project slug is different in kind: it never names a bank, it resolves through `projects.resolve` and `_authorize_resolution`, and it is exactly the value `_REPO_PATH` (`boot/prepare.py:71`) already validates as a trust boundary for the clone URL. Carry that validation over; do not invent a second one.

## 6. Flow

### Boot

1. Resolve the ach-memory endpoint (§9) and probe it. Unreachable → `MEMORY_DEGRADED`, no facade, run without memory. Fail-open, unchanged (D-02).
2. Start the ach-memory facade on loopback with the resolved credential and the tool allowlist.
3. **No mental-model provisioning.** `provision_memory`'s model loop is deleted. Bootstrap moves to the per-event path because it needs the project.

### Per event

Ordering already exists and is correct — `engine_runner` builds `ctx` from the channel event (`:123`) and only then calls `select_memory_wiring_async` (`:135`). Memory wiring is already downstream of the channel. Nothing to reorder.

1. Render `achMemory.project` from `ctx`. Validate against `_REPO_PATH`. Empty or invalid → user scope only, log, continue.
2. `POST /v1/bootstrap {project_slug}` — **once per project per process**, cached in memory. Not once per event. Fail-open: log, drop to user scope, continue.
3. `load_context(project_slug, scope)` — one call, replacing N× `get_mental_model`.
4. Take `ContextPayload.text` verbatim as the `## Memory` body. **Do not re-render sections** — `assemble_context` (`delivery.py:58`) has already ordered them, neutralised forged headings (`_inert`), applied every per-section budget and enforced the 5120-token global ceiling. The harness's whole job is `f"## Memory\n\n{payload['text']}"`. Log `omissions`, `overages` and `total_tokens`; put none of them in the prompt.
5. Pass the facade URL for this event to opencode.

### Passing the project to the facade

The facade is one long-lived loopback server shared across events; the tool call arrives from opencode on a different task than `engine_runner`, so a contextvar will not propagate. `engine_runner` already rebuilds `mcp_servers` per invocation (`:166`), so **put the project in the facade URL path** — `http://127.0.0.1:<port>/mcp/<urlencoded project>` — and have the facade read it per request. Stateless, per-event, no session map to leak or evict.

The alternative — a `{session_key: project}` map registered before each invocation — needs the MCP call to carry the session key, which it does not. Note the constraint the codemem comment records (`:174-176`): the pool reuses one agent per `session_key`, so anything baked at agent-acquire time is pinned by the first event of the session. A URL read per request is not.

### What the agent sees

Exactly as today: the agent never sees the credential, the endpoint, or the project. `retain`/`recall`/`reflect`/`get_mental_model` are exposed **without** `scope` and `project_slug` parameters; the facade fills both from the URL. This is the same contract as today's bank injection, and it is what makes §5's templating safe — the agent cannot choose, or be talked into choosing, another project.

## 7. Prompt

`TOOLS_SPEC` is per-backend and harness-appended (operator contract §2, line 516: *"each `memory.type` owns its own `TOOLS_SPEC`"*), so the ach-memory backend ships its own and no skill is needed in the catalog.

It must carry the typed-retain contract, because ach-memory rejects a retain that does not meet it: `memory_type`, `basis`, `trigger`, and 1–4 `evidence` excerpts are required, content is capped at 4 KiB, secrets are rejected, and content must be English regardless of conversation language. A retain missing any of these bounces on validation. This is boot-static harness text, not a hint the model can be argued out of.

Also drop from `gitlab-pr.yaml`'s system prompt: the `tags: ["repo:<project-path>"]` instructions and the `memory_recall`/`memory_reflect`/`memory_retain` tag guidance in Phases 1, 3 and 5. Scope replaces tags, and the harness sets it.

## 8. Deletions

| Delete | Where |
|---|---|
| `fetch_mental_model_summaries` | `memory/hindsight.py:177` (and the `detail="content"` fix, which goes with it) |
| Mental-model provisioning loop | `memory/hindsight.py:314-345` |
| `MentalModelSpec` | `config/schema.py:245`; `ach/api/ach/v1alpha1/achagent_types.go:75` |
| `HINDSIGHT_GET_MENTAL_MODEL`, `_CREATE_`, `_REFRESH_` | `memory/hindsight.py:36-39` |
| `memory_get_mental_model` facade tool | `memory/facade.py` |

Keep the Hindsight backend itself. `memory.type` is a discriminated union; ach-memory is a third arm beside `hindsight` and `codemem`, not a replacement of the union.

## 9. DECIDED — how the harness authenticates

**Decision (2026-09-09): direct in-cluster URL with an ach-memory user key.**

```yaml
achMemory:
  endpoint: http://ach-memory.ach.svc:8000
  auth:
    secretKeyRef:
      name: ach-memory-key
      key: ACH_MEMORY_API_KEY
```

The harness reaches both surfaces at one origin: `POST /v1/bootstrap` for provisioning and `/mcp` for `load_context` and the facade's proxied tool calls. Auth is `Authorization: Bearer <user key>` on both — the same header `proxy.py:97` already uses. `auth` is a `SecretSource`, resolved at use time, never logged, never forwarded to opencode: the existing `resolve_memory_secret` gate applies unchanged, including its `(False, None)` misconfigured-degrade branch.

**Why not the ek through the forwarder.** It was the other candidate: `achMemory.mcpServerId` naming a hydrated `runtime.mcpServers[]` entry, ek as `x-ach-key`, the forwarder minting the EdDSA JWT that `jwt_provider.py` already verifies. Rejected because it needs `bootstrap` on the MCP surface, which `FORBIDDEN_TOOLS` deliberately keeps off it (`create_project` is barred there), and that widening is not worth buying with this migration.

**Consequences to hold in mind:**

- **ach-memory requires no change for this work.** Plan A and this plan are fully independent and can run in either order or at once.
- **The memory principal is the key's user, not the ACH identity.** Every agent configured with the same key shares one user bank — so `user-context` is shared standing context across those agents. Give each agent its own ach-memory user key unless sharing is intended. With `scope: project` (§5) the user section is omitted anyway, which is the recommended setting for a bot.
- **A static shared secret stays in the agent's environment.** It is narrower than today's Hindsight admin secret — a user key is scoped to one ach-memory user and its authorized projects, not to every bank on the service — but it is still a credential to rotate, and rotation is a pod restart.

## 10. Test surface

Existing files to extend, following their conventions:

| File | Covers |
|---|---|
| `tests/config/test_memory_union.py` | the third union arm; `{type: ach-memory}` accepted, flat form rejected |
| `tests/config/test_ach_memory_schema.py` *(new)* | `tools` default set, unknown-tool refusal, `project` templating allowed, `scope` enum |
| `tests/memory/test_ach_memory_adapter.py` *(new)* | `load_context` → prompt section; bootstrap cached per project; every fail-open branch |
| `tests/memory/test_facade.py` | allowlist enforcement; `scope`/`project_slug` absent from every exposed signature |
| `tests/memory/test_wiring.py` | facade URL carries the rendered project; MCP server present/absent per probe branch |

Two behaviours worth a test of their own because they are the ones that will regress silently: **the agent-facing tool signatures must not contain `project_slug` or `scope`**, and **a project that fails `_REPO_PATH` must fall back to user scope rather than reaching `projects.resolve`**.

## 11. gitlab-pr.yaml migration

`aws-nglz-genai/gitops-genai-blueprint/workloads/agents/achagents/gitlab-pr.yaml`:

- replace the whole `memory:` block with §5's form
- delete the four `mentalModels` entries
- delete the `repo:<project-path>` tag instructions from Phases 1, 3 and 5 of the system prompt
- keep `GITLAB_TOKEN` and the prepare/cleanup scripts unchanged

No migration of the `gitlab-ackstorm` bank — declined. Its memories carry no `schema:ach-retain-v1` tag, so `project-context` (which matches `tags_match: "all"` on those tags) would read nothing from it regardless. Fresh start.
