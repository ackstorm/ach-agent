# ACH Agent Runtime — Shared Contract v3 (the seam)

> **Consolidated 2026-07-01:** this document folds in `CONTRACT_v3-ADDENDUM-hydration.md`
> and `CONTRACT_v3-ADDENDUM-prompt-source.md` and **supersedes them** — their corrected
> deltas (hydration writes, MCP auth headers, `x-ach-key` injection, the local egress
> forwarder decision, `prompt.system` typed source, the `.ach-state` root, the `memory`
> discriminated union) are now inline here. The two addendum files remain only as historical
> record. Codex-era specifics in the hydration addendum (`ach-cli`, `.codex/config.toml`,
> on-disk `ek_`, ephemeral tmpfs workDir) were superseded by the 2026-06-25 opencode pivot +
> localhost-proxy design and are intentionally NOT carried forward.

This document is the **single frozen interface** between `ach-runtime` (operator, Go)
and `ach-agent` (harness, Python). Both repos depend on this; neither may change it
unilaterally. Source of truth lives in `ach-runtime`; `ach-agent` pins a version.

Spec reference: `ach-agent-runtime-spec-v1_4_7.md` (API group `runtime.ackstorm.ai/v1alpha1`).

> **Frozen v1 — 2026-07-02.** The machine-readable half of §2 is the generated JSON Schema
> [`docs/schemas/agent-config-v1.schema.json`](../schemas/agent-config-v1.schema.json) —
> committed, published to the docs site at
> `https://ackstorm.github.io/ach-agent/stable/schemas/agent-config-v1.schema.json`, produced
> from the harness `AgentConfig` by `scripts/gen_schema.py`
> and drift-guarded by `tests/config/test_schema_artifact.py`. **That artifact — not this
> prose — is authoritative for field names, types, and defaults.** The
> `CONTRACT_v3-FOLLOWUP-memory-type.md` deltas are folded here (Resolved #10); its one
> remaining item is operator-side (`ach-runtime` renders `memory.type` from the CRD),
> tracked cross-repo. This freezes the **harness's accepted surface**; a full cross-repo
> freeze still needs `ach-runtime`'s rendered output diffed against this schema.

> v3 is a deliberate simplification of v2. Structural points:
> 1. **Egress is external MCP tools, not channel delivery.** `responseActions`, `inputSchema`,
>    `consentTier`, `webhook.deliver`, and `response` are **removed**. The agent acts by calling
>    **external MCP tool servers** (e.g. `gitlab-mcp`); the harness no longer dispatches actions.
> 2. **Channel set redrawn.** v1 = `webhook` (`source`-selected), `cron`, `queue`, `tui`, `a2a`.
>    No `slack`/`telegram`/`openai-compatible`. `gitlab` = `webhook` + `source: gitlab`.
> 3. **Engine is opencode (`opencode serve` + SSE), hardcoded; the `engine` block is removed;
>    `model` stays.** opencode is a complete, provider-agnostic agent with a config-driven MCP
>    client and structured output. The harness owns the `opencode serve` lifecycle and writes
>    `opencode.json` at hydration. The opencode bridge already exists in the harness
>    (`src/ach_agent/engine/`) and is reused, not rebuilt.
> 4. **Structured output is a fixed terminal contract** validated by the harness (Pydantic + ≤1
>    backstop retry). opencode returns best-effort structured JSON; the harness is the enforcer (§8).
> 5. **The harness fronts the model + MCP via a localhost proxy.** opencode points only at
>    `http://localhost/...`; the proxy injects the `ek_` (as `x-ach-key`) toward ACH. The `ek_`
>    never appears in opencode's config or environment (§3/§9).
> 6. **ACH context is skills / prompts / artifacts only (no plugins).** Each is a `tar.gz`
>    decompressed into a directory at hydration (§3).
>
> The router (§6) — dedup → backpressure → lane, the three finite bounds — is **unchanged**. It is
> the repo's IP. Harness language stays **Python**.

---

## 1. Direction of dependency (non-negotiable)

```
ach-runtime  ──renders──▶  rendered runtime config  ──read──▶  ach-agent
                          + ACH_* env (governed)
ach-agent    ── NEVER reads CRDs, NEVER talks to the API server, NEVER writes Agent.status
```

The harness has **no Kubernetes RBAC**. Status is the operator's job, derived from `pod.status`
only. The harness is tested against a hand-written rendered config.

### 1.1 Terminology (harness vs agente)

- **harness** — our Python process (`ach-agent`): channels, router, engine pool. It writes
  `opencode.json`, launches and supervises opencode, and owns all lifecycle/ordering.
- **agente** — an **opencode** process (`opencode serve`). The engine pool is **keyed by
  `session_key`**, so **one agente ⇄ one `session_key`** (1:1): each session identity (cron name,
  gitlab server+repo, tui-console) gets its own agente. All agentes share **one** `engine.home`
  (skills, `.ach-state`, session store, `node_modules` live once); per-`session_key` isolation is
  the opencode config file `<home>/.config/opencode/opencode_<session_key>.json` selected via
  `OPENCODE_CONFIG`. Distinct keys → distinct agentes (parallel, up to `maxConcurrentInvocations`);
  same key → same agente (serialized by the router lane, RTR-02).
- **`session_key`** — the stable, non-null identity a channel derives from its own facts (cron→name,
  gitlab→server+repo, tui→`tui-console`, future telegram→user). It is the **router lane key**, the
  **pool key**, and the reuse key for the opencode session (§2, `channel.session`). It is NEVER null
  (a null key would collapse all lanes/agentes into one).

---

## 2. The rendered runtime config (operator writes → harness reads)

One flat **JSON** file mounted at `/etc/ach-agent/config.json`. Machine→machine seam; the harness
validates with Pydantic v2 (`extra='forbid'`, `strict=True`) and hard-fails (`sys.exit(1)`) on any
mismatch.

```jsonc
{
  "schemaVersion": "1",                     // harness validates "1"; ach-runtime renders "1"
  "agent": { "name": "gitlab-ackstorm" },   // name only — namespace/generation removed

  // The engine is opencode, hardcoded. "engine" is a HARNESS-LOCAL block (how we run opencode:
  // home, workDir, startup deadline, env-forwarding allowlist) — the operator may render or omit it.
  // The harness writes opencode.json at hydration, pointing at the localhost proxy (§3/§9).
  // No "model.provider" (retired in v2).
  "model": {
    "name": "openai.gpt-5",                 // model id, passed verbatim; MUST be in hydrated models
    "type": "openai",                       // openai | gemini | anthropic — picks the ACH compat endpoint
    "params": { "temperature": 1 }          // OPEN, UNVALIDATED dict, splatted to the model client
  },
  "engine": {                               // harness-local; operator-optional
    "home": "/var/lib/ach-agent/home",      // ONE shared home for all agentes: skills + .ach-state +
    //                                         session store + node_modules live here. Per-session_key
    //                                         isolation = opencode config file selected via OPENCODE_CONFIG
    //                                         (<home>/.config/opencode/opencode_<session_key>.json).
    "workDir": "/var/lib/ach-agent/home/workspace",  // shared cwd (same for all agentes)
    "startupTimeoutSeconds": 30,
    "idleTtlSeconds": 30,                     // seconds an idle keyed agente lingers after its last
    //                                           release before being stopped. >0 keeps it warm so
    //                                           channel.session=auto persists the opencode session
    //                                           across events for the same session_key; 0 =
    //                                           spawn-per-invocation. Default 30.
    "forwardEnv": [],                        // extra env-var NAMES to forward into opencode; never the ek_
    "maxToolCalls": 0,                        // runaway control (Plan 4): abort a turn after N DISTINCT
    //                                           tool calls, then run one wrap-up turn so the model still
    //                                           returns a valid terminal object. 0 = OFF (default);
    //                                           recommend ~80 when opting in. maxInvocationSeconds
    //                                           (limits) stays the always-on time backstop.
    "type": "opencode",                       // opencode | pi. Selects the EngineDriver. Rendered
    //                                           by ../ach (AgentProfile.engine.type, free string);
    //                                           the harness is the enforcer (unknown → hard-fail).
    "pi": {                                   // PiEngineBlock; consulted only when type == "pi"
      "binaryPath": "pi",                     // pi on PATH in the image
      "mcpAdapterPath": "",                    // "" → image default:
      //                                          /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter
      "model": {                              // Pi-only model capability descriptor (models.json
        "reasoning": false,                    // fields). NOT sent to the model API call — that
        "input": ["text"],                     // stays model.params above (open, unvalidated,
        "contextWindow": 128000,               // per-call passthrough). Absent fields → these
        "maxTokens": 16384                      // same values (Pi's own builtin defaults).
      },
      "thinkingLevel": null                    // off|minimal|low|medium|high|xhigh|max; requires
      //                                           model.reasoning=true (hard-fail otherwise);
      //                                           passed to `pi` as --thinking at launch — never
      //                                           via settings.json defaults, never forced by the
      //                                           harness. Generated into
      //                                           docs/schemas/agent-config-v1.schema.json.
    }
  },
  // ── engine.type=pi is a CROSS-REPO contract ──────────────────────────────────
  // The ach-agent IMAGE must ship the `pi` binary + pinned pi-mcp-adapter BEFORE any
  // control plane renders engine.type=pi — otherwise the rendered config names a binary
  // the image lacks. Ship order: ach-agent image (Pi SP2) → then ../ach advertises pi.
  "capability": {
    "type": "ach",                          // ach only in v1 (direct is out — §7)
    "ach": { "baseUrl": "https://ach.ackstorm.ai", "environment": "engineering-prod" },
    "filter": {                             // gate ABOVE the model (withhold before offering)
      "exclude": {
        "tools": ["gitlab_merge_merge_request"],  // ids MUST match opencode's MCP tool names (boot probe logs them)
        "mcpServers": ["dangerous-admin"],
        "skills": ["send-email"]
      }
    }
  },
  "prompt": {                               // typed source; the bare-string shorthand is REJECTED
    "system": { "type": "text", "text": "…agent persona (markdown ok)…" },
    //   or { "type": "file", "file": "prompts/<name>/<file>.md" }  (relative to <home>/.ach-state; no "..")
    //   or { "type": "ach",  "ach": "<prompt-name>", "file": "<subpath>.md"? }  (harness resolves the file)
    "compose": "append"                     // contract-reserved (accepted, NOT yet executed)
  },
  "memory": {                               // null if not configured; fail-open (§6.5).
    // STRICT discriminated union on "type" — "type" REQUIRED, no legacy/no-type default.
    // Params live NESTED under memory.<type>.*.
    "type": "hindsight",                    // hindsight | codemem  (future: mem0, …)
    "hindsight": {
      "endpoint": "http://hindsight.engineering.svc:8080/mcp",
      "bank": "gitlab-pr-review",           // static bank_id, harness-owned; the agent NEVER
                                            //   sees or sets it. Per-repo split = tags, NOT
                                            //   templating (T-04-03).
      "auth": { "env": "ACH_SECRET_MEMORY_HINDSIGHT" }, // OPTIONAL (omit for internal/no-auth
                                            //   URL). Bearer admin secret, NOT the ek_. env-only.
                                            //   The OPERATOR generates the ACH_SECRET_* name
                                            //   (like ACH_SECRET_GITLAB_WEBHOOK); the author only
                                            //   picks the Secret + key. Unset-at-runtime → degrade.
      "mission": "AI code reviewer",        // optional; passed to create_bank at boot.
      "mentalModels": [                     // rich specs the harness PROVISIONS + reads at boot
        { "id": "architecture", "name": "Architecture",
          "sourceQuery": "What is the architecture?", "autoRefresh": true, "maxTokens": 2048 }
      ]
    }
    // FACADE: the agent reaches Hindsight ONLY through a harness-hosted localhost MCP server
    // exposing exactly four tools — memory_recall(query,tags?), memory_reflect(query,tags?),
    // memory_get_mental_model(id), memory_retain(content,tags?). No bank_id parameter, no
    // admin/destructive tools. The harness injects bank_id + the admin Bearer per call; the
    // raw Hindsight endpoint and the admin secret never reach opencode. Provisioning
    // (create_bank + create/refresh mental_model) runs boot-once, fail-open.
    // codemem variant: { "type": "codemem", "codemem": { "dbPath": "…", "project": "…" } }
    //   dbPath: absolute, no ".." (a local stdio MCP, model-managed); omit → derived from
    //           persistence (<mountPath>/codemem/codemem.db, else /tmp/ach-home/…).
    //   project: CODEMEM_PROJECT for the agente's codemem child; default fixed "ach-agent"
    //           (no magic). TEMPLATABLE (see below).
    //
    // TEMPLATING of memory.codemem.project: same {{ }} engine and namespaces as channel.prompt
    // (payload.*, internal.*). Rendered by the HARNESS in engine_runner with the FULL triggering
    // event context, then baked into opencode.json when the agente launches.
    // (memory.hindsight.bank is STATIC — the facade captures it once at boot; use tags for
    //  per-repo partitioning, never a templated bank.)
    // Because the agente is 1:1 with session_key and is REUSED across that key's events, the value is
    // captured from the FIRST event that launched the agente and fixed for its lifetime — so a
    // templated bank/project MUST be invariant per session_key (e.g. "{{ internal.session.key }}",
    // or a payload field that IS the session identity like the telegram user's email). A per-event
    // field that varies within a session_key would go stale.
  },
  "mcpServers": {                           // harness-managed MCP servers (map keyed by name),
    // STRICT discriminated union on "type". Distinct namespace from runtime.mcpServers (hydrate's
    // ACH-fronted {id,endpoint} externals) — no collision. Empty/omitted → no extra MCP wiring.
    "repo-checkout": {                      // INTERNAL: the harness HOSTS it (FastMCP facade + ek_)
      "type": "repoCheckout",               //   exposes checkout_repo(project,ref,subpath?); see §9.
      "repoCheckout": {                     //   params nested (built-in "special" wiring)
        "sourceMcpServerId": "mcp-gitlab-ro",  // which hydrated runtime.mcpServers[].id serves the
        //                                        gitlab://{project}/archive/{ref} resource the
        //                                        harness reads (with the ek_, harness-side).
        "tmpBase": "/tmp/gitlab",           //   parent dir for per-checkout mkdtemp dirs (default)
        "ttlSeconds": 3600                  //   stale-checkout sweep TTL, >= 0 (repoCheckout-only)
      }
    },
    "filesystem": {                         // PASSTHROUGH local: opencode LAUNCHES it (stdio subprocess).
      "type": "local",                      //   opencode is the MCP client — connects DIRECTLY (no proxy).
      "command": "docker",
      "args": ["run","-i","--rm",
               "--mount","type=bind,src=/data/desktop,dst=/projects/desktop",
               "mcp/filesystem","/projects"],
      "env": []                             //   env NAMES to forward (never the ek_); optional
    },
    "other": {                              // PASSTHROUGH remote: opencode CONNECTS directly
      "type": "remote",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:OTHER_MCP_TOKEN}" }  // ${env:NAME} refs, not values
    }
  },
  "limits": {
    "maxConcurrentInvocations": 8,
    "maxInvocationSeconds": 1800,
    "maxQueuedTotal": 100,
    "idempotencyWindowSeconds": 3600,
    "maxSteps": 50,
    "terminalOutputRetries": 1              // §8 — harness validates + ≤1 backstop retry
  },
  "persistence": { "enabled": true, "mountPath": "/var/lib/ach-agent" },
  "health": { "host": "0.0.0.0", "port": 8000 },
  "channels": [
    { "name": "gitlab-mr-review", "type": "webhook", "source": "gitlab",
      "concurrency": 4,                       // per-channel sub-cap under maxConcurrentInvocations
      "session": { "type": "none" },          // SessionBlock | string shorthand. BREAKING (v3→v3.1):
      //   default changed auto → none. type ("auto"|"none"|"custom", default "none") is the
      //   discriminator for which opencode session a turn reuses:
      //     "none"   = fresh session per event, deleted post-turn (no residue).
      //     "auto"   = the channel-derived session_key (per-MR for gitlab/github, channel name for
      //                cron/queue, context_id for a2a) — conversational continuity, same ses_ reused.
      //     "custom" = reuse the session under `key`, a {{ }} template rendered per event
      //                (payload.*, internal.*; header.* reserved, empty). Empty render → none
      //                behavior + WARN (never a shared "" key).
      //   key (string): REQUIRED iff type=="custom", FORBIDDEN otherwise (the {{ }} template).
      //   maxTokens (int>0, optional): once the previous turn's input_tokens exceed it, apply
      //   overflow (auto/custom only; ignored for none). overflow ("compact"|"rotate", default
      //   "compact"): compact summarizes the session in place (POST /session/{id}/compact);
      //   rotate starts a fresh session and deletes the old one.
      //   Shorthand: "session": "auto" ≡ {"type":"auto"}; "none" ≡ {"type":"none"}; any other
      //   string (a template) ≡ {"type":"custom","key":"<str>"}.
      //   This toggles ONLY which opencode session is reused — it does NOT change session_key:
      //   the router lane + pool key stays stable ("none" ≠ null key). Recommended: auto for
      //   gitlab/github and a2a channels; none (default) elsewhere unless conversational memory
      //   is wanted.
      // channel.prompt is rendered with {{ }} substitution. Namespaces: payload.* (the
      // inbound JSON body), internal.* (channel.name|type|source, agent.name, memory.bank,
      // event.id, session.key). One filter: | default("x"). No env namespace (ek-hygiene).
      // The SAME engine + namespaces render memory.hindsight.bank / memory.codemem.project (§2 memory).
      "prompt": "Review this merge request: {{ payload.object_attributes.url }}",
      "webhook": { "auth": { "type": "gitlab_token",
                             // secret: {env: NAME} — env-only (no disk secrets). The harness reads
                             // os.environ[NAME] at use time; dumpable=0 + opencode's clean-slate env
                             // keep it from the agent. NAME MUST NOT be in engine.forwardEnv. A {file}
                             // source is NOT supported and is rejected at config load (extra_forbidden).
                             "secret": { "env": "ACH_SECRET_GITLAB_REVIEW_WEBHOOK" } },
                   // gitlabEvents: which GitLab event kinds THIS channel ROUTES to the agent.
                   //   Omit / null → all routable kinds ["merge_request","issue","note"].
                   //   Routable kinds yield a per-conversation session_key:
                   //     merge_request hook & MR-comment → "{project}:{mr_iid}"    (shared lane)
                   //     issue hook & issue-comment       → "{project}:issue:{iid}" (namespaced)
                   //   A note (comment) routes only when "note" AND its noteable base kind
                   //   (merge_request/issue) are both listed. Kinds not listed — and non-routable
                   //   kinds (push, tag_push, pipeline, emoji, …) — are accepted-and-IGNORED
                   //   (HTTP 200 {"status":"ignored"}), NEVER 422, so GitLab does not auto-disable
                   //   the hook. 422 is reserved for a routable-but-malformed payload only.
                   "gitlabEvents": ["merge_request", "note"],
                   // botUsername: the GitLab username the agent posts AS (the egress PAT's user —
                   //   a DISTINCT fact from agent.name). When set, inbound events authored by this
                   //   user, plus gitlab-generated system notes, are dropped pre-enqueue (HTTP 200
                   //   {"status":"ignored"}) — the harness loop-guard, so the agent never
                   //   re-triggers on its own comments/MRs. Omit/null → guard off (agent must
                   //   self-guard via prompt). gitlab source only.
                   "botUsername": "ackbot",
                   // triggerUsers: actor allowlist — only these GitLab usernames may trigger the
                   //   agent (applies to EVERY routed kind: mr/issue/note). Omit/null → any author
                   //   triggers. Non-listed authors are dropped pre-enqueue (HTTP 200 ignored).
                   //   gitlab source only.
                   "triggerUsers": ["juancarlos.moreno", "ivan.hernanz"] } },

    { "name": "generic-hook", "type": "webhook", "source": "generic",
      "webhook": { "auth": { "type": "header_token",   // static shared secret in a configurable header
                             "header": "X-Api-Key",
                             "secret": { "env": "ACH_SECRET_GENERIC_HOOK" } } } },

    { "name": "daily-security", "type": "cron", "concurrency": 1,
      "cron": { "schedule": "0 8 * * 1-5", "timezone": "Europe/Madrid" },
      "prompt": "Scan main for new CVEs; open an issue via your tools if any are critical." },

    { "name": "ticket-triage", "type": "queue", "concurrency": 2,
      "queue": { "type": "redis", "key": "ach:triage", "ackMode": "onComplete" },
      "prompt": "Triage this ticket and act via your tools." },

    { "name": "peer-intake", "type": "a2a", "concurrency": 2,
      "a2a": { "mode": "async",
               "auth": { "header": "x-a2a-custom-api-key",
                         "secret": { "env": "ACH_SECRET_PEER_INTAKE_A2A" } } } }
  ]
}
```

**`tui` is NOT a channel** — it is the `--tui` launch modifier (console mode that ignores the
configured channels and attaches to opencode's native TUI). It has no terminal contract (§8) and
is absent from the `channels[]` list and from the channel-type union.

**The `engine` block** is harness-local (`home`, `workDir`, `startupTimeoutSeconds`, `idleTtlSeconds`,
`forwardEnv`) — the
operator may render or omit it; opencode itself is hardcoded and configured via `opencode.json` at
hydration. `home` and `workDir` default from `persistence.mountPath` (enabled → `<mountPath>/home`,
`<home>/workspace`) or `/tmp/ach-home` (disabled). opencode's subprocess env is built clean-slate
from a small base allowlist; `forwardEnv` names extra vars to forward — **never the `ek_`**
(`ACH_TOKEN`/`ACH_API_KEY` are never forwarded).

**On-disk hydration layout** (pinned; see §3):
- Skills hydrate into `<home>/.config/opencode/skills/<name>/SKILL.md` — the only path opencode
  scans (skill discovery is not configurable via `opencode.json`). This dir is **reconciled**
  (wiped, then re-extracted) on every boot, so the on-disk skill set always equals the current
  post-exclusion manifest.
- Prompts and artifacts hydrate under the single state root `<home>/.ach-state/`:
  `<home>/.ach-state/prompts/<name>/` and `<home>/.ach-state/artifacts/<name>/`.

**`prompt.system` is a typed, discriminated source** — `type` is required; the plain-string
shorthand is **REJECTED** (`extra=forbid` + discriminator → `ValidationError`):
- `{ "type": "text", "text": "…" }` — inline persona.
- `{ "type": "file", "file": "prompts/<name>/<f>.md" }` — path relative to `<home>/.ach-state`;
  absolute or `..` is rejected.
- `{ "type": "ach", "ach": "<prompt-name>", "file"?: "<subpath>.md" }` — persona from a hydrated
  prompt addressed by NAME; the harness resolves the file (the prompt dir's sole file, or the
  optional `file` subpath). Preferred form — the operator names the prompt, not its on-disk path.

`compose` is contract-reserved (`"append"` accepted, not yet executed). Resolution + security are
in §3.

**Webhook auth types:** `gitlab_token` (plain X-Gitlab-Token compare), `hmac` (GitHub-style
X-Hub-Signature-256), `header_token` (static shared secret in a configurable `header`), `none`.
**`filter.exclude.tools`** ids must match opencode's MCP tool names — the boot tool-probe logs each
server's tool names (`log.info("mcp tools", …)`); copy those ids verbatim.

**Removed vs v2 (do not render):** `agent.namespace`/`agent.generation`, top-level `governed`,
`workDir`/`startupTimeoutSeconds` at the root (moved under `engine`),
`channels[].session`, `channels[].expire`, `channels[].responseActions`, `channels[].response`,
`channels[].webhook.deliver`/`deliverOnly`, any `inputSchema`/`consentTier`, `model.provider`, and
**`agentEnv`** — opencode runs behind the localhost proxy; there is no subprocess credential env to
inject (the proxy holds the `ek_`). The bare-string `prompt.system` form is also removed.

**Secrets (inbound channel auth):** each `auth.secret` is `{env: NAME}` — **env-only, no disk
secrets** (`extra='forbid'`; a `{file: PATH}` source is rejected at config load). The config carries
a **name, never a value**. The operator injects the Secret as a container env var
(`env[].valueFrom.secretKeyRef`) and renders `{env: NAME}`. The harness reads `os.environ[NAME]` at
use time; it is protected from the co-resident agent because the harness sets `PR_SET_DUMPABLE=0`
(so `/proc/<harness>/environ` is unreadable by opencode) and builds opencode's env clean-slate, so
the value never reaches the engine. **The `env` NAME MUST NOT appear in `engine.forwardEnv`** — if it
does, the harness **strips it from the forwarded set and logs a WARN** at boot (fail-safe: the secret
never reaches the engine even on a misconfig). The `env` name must be a valid environment variable
identifier (`[A-Za-z_][A-Za-z0-9_]*`); the operator sanitizes it and the harness validates.
Rationale for env-only: a file-mounted Secret is **same-uid readable by opencode** (`dumpable=0` does
not cover disk); env injection is the only source that keeps inbound-auth secrets off the agent.
The GitLab **egress** token still lives in the `gitlab-mcp` server's config (egress is the MCP's
job, §9), not in ach-agent — this section is inbound auth only.

---

## 3. The ACH_* env contract + hydration

When `governed: true`, the operator materializes these into the main container env:

| env var            | value                                                  | notes |
|--------------------|--------------------------------------------------------|-------|
| `ACH_BASE_URL`     | `CapabilityProfile.ach.endpoint`                       | fronts ALL egress — model, MCP, outbound A2A |
| `ACH_TOKEN`        | the `ek_` (from `Agent.capability.identity.secretRef`) | ACH credential; held ONLY by the harness proxy; injected as the `x-ach-key` header; never logged; never reaches opencode |
| `ACH_ENVIRONMENT`  | `CapabilityProfile.ach.name`                           | which ACH Hub Environment; sent as `x-ach-environment` on egress and used at boot self-hydration |

The `ek_` is **only** ever in `ACH_TOKEN`, held by the harness. **ACH's auth scheme is the
`x-ach-key` header, NOT `Authorization: Bearer`** (a bearer 400s/401s against the ACH endpoint).
The proxy pairs it with `x-ach-environment` on egress. Rotation by secret-hash restart.

**Hydration (no init container, no CLI).** At boot the harness self-hydrates from the single config,
calling ACH's HTTP hydrate endpoint directly (it does **not** shell out to a CLI):

1. **Hydrate** — the harness calls `POST {ACH_BASE_URL}/platform/hydrate` with `x-ach-key: ek_`.
   Manifest: `runtime.models[{id,endpoint}]`, `runtime.mcpServers[{id,endpoint}]`,
   `runtime.a2aAgents[{id,endpoint}]`, `context.{skills,prompts,artifacts}[{name,downloadUrl}]`.
   The Environment's **direct runtime** (models / mcpServers / a2aAgents) — the governed-ACH egress
   the whole v3 design rests on — is the source for all three runtime axes; the harness does not
   hand-write a model/provider block.
2. **Resolve the model (fail-closed)** — `model.name` MUST be in `runtime.models[]` → else hard-fail
   at startup. The Environment returns the menu (litellm-discovered); the operator-rendered
   `model.name` picks one of it. The operator chooses the model; the Environment bounds the choice.
3. **Start the localhost proxy** (§9): a local reverse-proxy exposing `http://localhost/v1`
   (and `/gemini`, `/anthropic`) for the model, and local MCP routes (`/mcp/<id>`) for the
   provisioned servers. The proxy injects `x-ach-key: ek_` (+ `x-ach-environment`) toward
   `ACH_BASE_URL`, streaming SSE transparently. The model wire may override the auth header where a
   provider requires it, but the `ek_` still lives only inside the proxy.
4. **Fetch context** — download each `skills/prompts/artifacts` `tar.gz` (with `x-ach-key: ek_`)
   and safe-extract (traversal-checked) into its directory: skills → reconciled into
   `<home>/.config/opencode/skills/<name>/`; prompts → `<home>/.ach-state/prompts/<name>/`;
   artifacts → `<home>/.ach-state/artifacts/<name>/` (see the `.ach-state` root below).
5. **Resolve `prompt.system`** (below) and materialize the persona to
   `<home>/.config/opencode/personality/system_prompt.txt`, referenced from `opencode.json`
   `instructions: [...]` (append).
6. **Write `opencode.json`** pointing the model `baseURL` and the `mcp` servers at **localhost**
   (no `ek_`, no real ACH URLs), apply `capability.filter.exclude`, then start `opencode serve`.

A hydration failure is an ordinary startup failure (exit within `startupTimeoutSeconds` → pod
not-ready), not a dedicated condition. Hydration runs **before** the router path, at boot.

> **Non-normative:** `ACH_STATS_*` environment variables (`ACH_STATS_REDIS_URL`,
> `ACH_STATS_RETENTION`, `ACH_STATS_TZ`) are **harness-local** and explicitly **outside this
> contract**. They configure the optional stats sink / stats service and are not rendered by the
> operator. Promote to a real config block only if eval infrastructure (Sub-project B) requires
> operator awareness.

### 3.1 The `.ach-state` hydration root

Hydration writes three kinds with different consumers, consolidated under one root in the engine HOME:

```
<home>/.ach-state/prompts/<name>/…       # harness-consumed (persona layering); the agent does not need it
<home>/.ach-state/artifacts/<name>/…     # agent working material (e.g. ach-cr-samples)
<home>/.config/opencode/skills/<name>/   # UNCHANGED — opencode scans this exact path; not movable
```

- **`ACH_STATE = <home>/.ach-state`.** `home` resolves as in §2 (`engine.home`, else
  `<mountPath>/home` when persistence is enabled, else `/tmp/ach-home`).
- **Why HOME, not workDir:** `workDir` is the agent's mutable cwd (it clones repos, writes scratch);
  hydrated state is read-only, kept out of that churn. HOME already holds skills. The `.`-prefix
  keeps it out of the agent's workspace `ls`.
- **Agent shell access to artifacts:** if `workDir != home`, the harness symlinks
  `<workDir>/.ach-state` → `<home>/.ach-state` so the agent reaches artifacts at one stable path.

### 3.2 `prompt.system` resolution + security (harness)

One materialization path for all forms: the resolved bytes are written to
`<home>/.config/opencode/personality/system_prompt.txt` and referenced via `opencode.json`
`instructions` (append). `{ "type": "text" }` and the removed string form are byte-identical in effect.

**Memory tools spec (harness-appended).** When `memory` is configured, the harness appends a
per-backend `## Memory Tools` section to the resolved system prompt — a short, static description of
how to use that backend's tools (hindsight's `memory_recall/reflect/retain/get_mental_model`;
codemem's `memory_search/timeline/pack/remember/forget/get_observations`). It is **boot-static**
(the backend is known at boot; keeps the system-prompt prefix stable → prompt-cache friendly) and
lives with the backend (each `memory.type` owns its own `TOOLS_SPEC`). This is distinct from the
per-invocation `## Memory` content block hindsight appends to the event prompt (mental-model
summaries + their ids). The hindsight tools take **no `bank_id`** — the harness facade injects it.

For `{ "type": "file", "file": F }`:
1. Resolve `F` relative to `ACH_STATE`.
2. **Reject** (hard validation error) if `F` is absolute or the resolved real path escapes
   `ACH_STATE` (any `..` traversal) — enforced structurally in a `field_validator` at load AND
   re-checked against the resolved `ACH_STATE` at read time. A `file:` is operator/agent-spoofable
   and MUST NOT read arbitrary disk (e.g. secret files) into the system prompt.
3. **Error** (startup failure within `startupTimeoutSeconds`) if the file is missing — a declared
   persona that hydration did not deliver is a misconfiguration, NOT a fail-open case.

For `{ "type": "ach", "ach": A, "file"?: F }`:
1. Resolve the prompt dir `<ACH_STATE>/prompts/A` (`A` rejected as absolute/`..` at load; a `/`
   inside `A` is allowed as a nested registry-qualified name, it just cannot escape upward).
2. **Error** (startup failure) if that dir is not hydrated.
3. Pick the file: if `F` is given, `<prompt-dir>/F`; else the dir's **sole** file, searched
   recursively — **error** if the dir has 0 or >1 files (the log lists them so the operator can add
   an explicit `file:`).
4. Then the same read-time containment + missing-file behaviour as `file` above.

---

## 4. HTTP endpoints the harness MUST expose

```
POST /channels/{channelName}/events    # inbound for HTTP-delivered channels (webhook, a2a)
GET  /healthz                          # liveness
GET  /readyz                           # readiness = all enabled channel adapters listening
GET  /metrics                          # Prometheus
```

`readyz`: Ready when adapters listen. Engine warmup is NOT a readiness gate, but if the engine
does not reach ready within `startupTimeoutSeconds` the process exits.

**Webhook acceptance is decoupled from engine state (2026-07-02).** Inbound-channel acceptance
(webhook, a2a) depends ONLY on harness readiness (routes up) and `draining` (SIGTERM straggler
503) — NEVER on engine/pool state. The pool is keyed by `session_key`, which is unknown until
the message is parsed, so a global "engine ready" precondition is a category error (see
`docs/references/2026-07-01-router-pool-vs-legacy.md`, finding B8): the engine only starts
lazily, per key, inside the lane on first acquire. A successful webhook returns `202` with
`{"status":"accepted"}`, plus an optional correlation header `X-ACH-Task-Id` (and matching
`task_id` in the body) — a logged handle only, NOT persisted, NOT queryable. Outcome surfaces
via the agent's own MCP actions plus harness logs/metrics (`ENGINE_LAUNCH_FAILURES` on a failed
lazy start), not a synchronous HTTP status and not (yet) a task-status API.

---

## 5. Status conditions — operator-only

The harness populates **none**. Post-start / per-invocation failures are **telemetry**, never status.

---

## 6. Behavioral invariants the harness MUST honor (conformance tests)

1. **Idempotency-key derivation:** per channel type — webhook header chain → ms-timestamp
   fallback; queue message id; a2a task id; cron `{channel}:{scheduled_tick_time}`. NEVER a
   shared/empty key.
   - **GitLab secondary dedup key (2026-07-02):** for `source == "gitlab"` the webhook channel
     ALSO derives a **logical content composite** `gl:{kind}:{project}:{target}:{user}:{content_hash}`
     (action-excluded, content-sensitive) as `MessageEvent.secondary_idempotency_key`. The router
     checks & marks it ALONGSIDE the primary UUID key inside the dedup step (before backpressure —
     it must not consume a queue slot). It uses a **SHORT window** (`_SECONDARY_DEDUP_WINDOW_S = 2s`),
     NOT `idempotencyWindowSeconds`: it collapses GitLab's near-simultaneous logical double-fires
     (`open`+`update`, fresh-UUID re-delivery) the UUID key misses, without deduping intentional
     identical comments minutes apart. `None` for every non-gitlab channel (unchanged behaviour).
2. **Pre-lane order: dedup → backpressure (maxQueuedTotal) → lane.**
3. **Three finite bounds always enforced:** maxConcurrentInvocations, maxInvocationSeconds, maxQueuedTotal.
4. **`expire` exhaustion / full queue is never silent:** 503 sync / NACK-redelivery / drop-log.
5. **Memory is fail-open:** backend down → run without memory context, log it, never fail.
6. **Startup deadline:** engine/hydration not ready within startupTimeoutSeconds → exit.
7. **~~Proven-start gate A′~~ RETIRED (2026-07-02):** acceptance is decoupled from engine
   readiness. Inbound-channel acceptance depends ONLY on harness readiness + `draining`, NEVER
   on engine/pool state — a global "engine ready" latch is a category error against a pool keyed
   by `session_key` (unknown until parse time). The engine starts lazily, per key, on first
   acquire inside the lane; a failed lazy start is surfaced as an explicit
   `ENGINE_LAUNCH_FAILURES` metric + WARN, never a silent drop (RTR-05 preserved). See
   `docs/references/2026-07-01-router-pool-vs-legacy.md`, finding B8.
8. **Self-hydration at boot:** resolve Environment + provision MCP set + start localhost proxy +
   write opencode.json before opencode serves; failure = startup exit, never a silent half-start.
9. **Egress is the agent's via MCP, not the channel's:** the channel never posts on the model's
   behalf; it only ingests events and (for call channels) returns the terminal result.
10. **Secret hygiene:** the `ek_` is held only by the harness/proxy. It MUST NOT appear in
    `opencode.json`, opencode's env, logs, or any model/MCP request opencode can observe. opencode
    points only at `http://localhost/...`; the proxy adds `x-ach-key` toward ACH.

---

## 7. What is NOT in v1 (declared, do not build)

- Channels: `slack`, `telegram`, `openai-compatible`, sync `a2a`, board, hooks.
- **Hermes dependency dropped** from `pyproject.toml` (was only for slack/telegram).
- **Plugins.** ACH context in v1 is **skills / prompts / artifacts only**. Plugin bundles
  (skills + subagents + mcps + hooks) are **not supported** — no plugin explosion, and no
  plugin-contributed MCP auth classes (e.g. `x-litellm-api-key` passthrough) in v1.
- O2/O4/O7/O8, `/v1/responses` facade, per-channel `rateLimit`.
- **Consent / tool-limiting (O9, v1.1):** the `consent` terminal action (§8) is reserved but
  non-executable in v1. Real enforcement is tool provisioning (`capability.filter`, §9).
- **`queue`: redis only in v1.**

Keep the channel→router boundary a **named in-process seam**.

---

## 8. Structured output — the terminal contract

**v1 = text-based extraction.** The prompt asks the model to end with a terminal JSON object;
opencode streams its answer as **text** over SSE (the model may also emit free prose — see the
`text` field). **The harness is the enforcer:** it extracts the JSON from the accumulated text,
validates it against the channel-class Pydantic model, and on a miss does at most one backstop
retry (`terminalOutputRetries`), then follows the table. This is the existing `validator.py` path.
opencode's native `format: json_schema` is a future optimization, not used in v1, and would still
require the same harness validate-+-retry backstop.

**Tools vs terminal output are different.** During the run the agent calls **MCP tools** (egress,
§9). The terminal JSON is only the **end-of-turn signal**, not how the agent "talks".

**SSE resilience (harness-internal, no config).** The live consumer sends the prompt exactly
once, then consumes the SSE stream to `session.idle`. A transient reader drop mid-turn is
recovered by a bounded (3), health-gated reconnect (re-subscribe only — never re-send;
opencode resends cumulative snapshots and the harness prefix-dedups them). A mid-turn engine
death is detected within ~5s (a liveness poll) and fails the invocation fast (`engine_died`)
instead of waiting the 300s stall bound. All bounded from above by the lane's
`maxInvocationSeconds`. Constants are harness-internal (`_LIVENESS_POLL_S`, 300s stall); no
config field.

| Channel class | Channels | Terminal contract | If invalid after retries |
|---------------|----------|-------------------|--------------------------|
| **async (no result expected)** | webhook, cron, queue | `{"action":"none","text":"…","thoughts":"…"}` | log + **ignore** (work already done via tools) |
| **call — async result** | a2a (async-only) | `{"action":"a2a_reply","text":"…"}` | **callback FAILED** to the caller (`TaskStatusUpdateEvent(state=failed)`) |
| **call — free** | tui (`--tui` modifier) | **none** — stream text to the terminal | n/a |

Terminal action models (Pydantic discriminated union — **single object, NOT a list**). Every action
carries a free-text `text` field so the model always has a place to "finish" with natural language:

```python
class NoneAction(BaseModel):      action: Literal["none"];      text: str = ""; thoughts: str = ""
class A2AReply(BaseModel):        action: Literal["a2a_reply"]; text: str;      thoughts: str = ""
class ConsentRequest(BaseModel):  action: Literal["consent"]    # RESERVED, v1.1
    tool: str; args: dict = {}; reason: str = ""
# TerminalAction = NoneAction | A2AReply | ConsentRequest | …
```

---

## 9. Tools / egress — external MCP via the localhost proxy

The agent acts by calling **external MCP tool servers** (e.g. `gitlab-mcp`). opencode is a
config-driven MCP client — **but it points only at the harness's localhost proxy.** The proxy
fronts the ACH-fronted MCP servers, injecting the `ek_` (as `x-ach-key`, with `x-ach-environment`).
opencode never sees the `ek_` or the real ACH URLs.

**Where the tool set comes from (governed `type: ach`, the only v1 path):**
- Hydration returns `runtime.mcpServers[{id, endpoint}]`. The harness creates a **localhost route**
  (`/mcp/<id>`) per server and writes it into `opencode.json` (`mcp.<id> = {type: remote,
  url: http://localhost/mcp/<id>}`).
- The ACH Forwarder fronts all egress (model, MCP, outbound a2a). The localhost proxy strips any
  client-supplied auth and adds the ACH `x-ach-key` + `x-ach-environment` — **no per-MCP
  credentials, no real ACH URLs, and no `ek_` in `opencode.json`.**

**The local proxy is a credential-injection shim, not an egress re-implementation.** It is a dumb
header-injecting passthrough in front of the *remote* ACH Forwarder (two forwarders in series; the
local one exists **only** to keep the `ek_` out of the agent). It does **not** re-implement egress
semantics (no Hermes `send_*`, no in-process MCP business logic, no posting on the model's behalf).
It is also the natural **enforcement + observability choke point**: it can reject a withheld MCP
tool call at the proxy (complementing provisioning) and emit per-invocation egress telemetry.

> **Isolation caveat (residual, honest).** In v1 the harness runs **same-process at the same uid**
> as opencode. Two theft surfaces, and their current status:
> - **process env + memory (`/proc/<harness>/{environ,mem}`, ptrace):** CLOSED. A boot preflight
>   sets `PR_SET_DUMPABLE=0` (reowns those to root, blocks same-uid ptrace) + `PR_SET_NO_NEW_PRIVS=1`
>   (inherited by opencode), and fails closed on an unsafe host (not-root / no `CAP_SYS_PTRACE` /
>   no `CAP_SYS_ADMIN`; override `ACH_INSECURE_ALLOW_DEGRADED=1` for dev). The `ek_` (env `ACH_TOKEN`)
>   and every `auth.secret` (env-only) are therefore unreadable by the agent.
> - **disk secret files:** NOT a surface for inbound-auth secrets — `auth.secret` is **env-only**
>   (a `{file:…}` source is rejected at config load), so no inbound-auth secret is ever written to a
>   path opencode can read. This deliberately closes the same-uid disk-read gap that a file source
>   would have left open (`dumpable=0` does not cover disk). Any secret that a future feature *must*
>   place on disk would need a **distinct uid** for opencode (Tier 2, `CAP_SETGID`/`CAP_SETUID`) or a
>   **sidecar/second container** (Tier 4) — a v1.1 item. The cleanest upstream fix for the `ek_` —
>   ACH minting a short-lived, pod-scoped, audience-bound `ek_` per hydrate — is an open question for
>   the ACH team, not a harness requirement.

**a2a egress = harness-hosted MCP tools.** Peer agents (`runtime.a2aAgents`) are surfaced to the
model as MCP tools `a2a_{name}` / `a2a_{name}_async` / `a2a_{name}_status` (ported from ackbot
`handlers/a2a/{tools,client,notification_store}.py`, a2a-sdk client). It is one of the harness's
**hosted** MCP servers (the others: the memory facade §6.5, and the `checkout_repo` tool below);
everything else is a proxied remote server. (Distinct from the inbound `a2a` **channel**, which
receives calls — `channels/a2a.py`.)

**Repo checkout = harness-hosted MCP tool (opt-in, `mcpServers[].type=repoCheckout`).** When
declared, the harness hosts a localhost MCP server exposing one tool, `checkout_repo(project, ref, subpath?)`,
so the agent can get an **on-disk** repo tree (full-tree `rg`, run tests, build) — things a
per-file MCP call can't give. The harness reads gitlab-mcp's `gitlab://{project}/archive/{ref}
[/{subpath}]` **resource** itself (opencode is an MCP client but discards resource blobs),
authenticating with the `ek_` as `x-ach-key` harness-side, base64-decodes the gzip tar, and
extracts it under `repoCheckout.tmpBase` (path-traversal-safe via `tarfile` `filter="data"`),
returning the on-disk path. `sourceMcpServerId` names which hydrated `runtime.mcpServers[].id` serves the
archive resource — so this rides the existing gitlab MCP provisioning, no new egress surface. It is
**fail-soft**: a failed checkout (over-cap / GitLab 403/404) returns an error string, never raises.
Cleanup is TTL-swept on the NEXT call (`ttlSeconds`), not session-close (Option A — `/tmp` is
ephemeral). The gitlab MR/note channel stamps `head_sha` into the delivery context, and the engine
prompt gets a one-line `checkout_repo(project=…, ref=…)` hint only when the facade is wired AND a
head SHA is present. Requires gitlab-mcp to actually serve the archive resource (behind
`GITLAB_REPO_ARCHIVE=1`); until then, leave the `repoCheckout` entry out of `mcpServers`.

**The tool-limiting / consent gate is provisioning, not validation.**
`capability.filter.exclude.{tools,mcpServers,skills}` **withholds** capabilities **before** they
are offered to opencode — the model literally cannot call/see them. The reserved `consent` terminal
action is the agent **requesting** an unlock (v1.1). A compromised agent's blast radius is bounded
by `capability.filter` (the provisioning gate), not by hiding the key.

**Failure mode (honest):** a down MCP server means the agent cannot perform that action. Unlike
memory (fail-open), tool egress is not fail-open — surface it as a per-invocation telemetry failure.

---

## Resolved

1. **queue** — redis only in v1; idempotency key = redis message id. (2026-06-25)
2. **MCP servers** — provisioned from hydration, fronted by the localhost proxy; no explicit
   server list and no real URLs in the config. (2026-06-25)
3. **a2a async callback** — caller-supplied (rides in the inbound request). Our config only
   validates the inbound caller (`a2a.auth`). a2a **egress** peers come from `runtime.a2aAgents`. (2026-06-25)
4. **cron timezone** — IANA `timezone` field. (2026-06-25)
5. **Forwarder** — fronts MCP, A2A-client, and models uniformly, via the harness localhost proxy;
   it is a credential-injection shim (§9), a conscious, bounded reversal of "the harness never
   wraps egress" (it wraps transport only, to keep the `ek_` out of opencode). (2026-06-24)
6. **Engine = opencode (`opencode serve` + SSE), hardcoded.** The bridge already exists in
   `src/ach_agent/engine/` and is reused. `engine` block removed; `model{name,type,params}` stays.
   Structured output is harness-validated (text extract + Pydantic + ≤1 retry). Router IP + tests kept. (2026-06-25)
7. **Secret hygiene** — the harness fronts model + MCP on localhost; the `ek_` never reaches
   opencode. ACH auth is the `x-ach-key` header (+ `x-ach-environment`), NOT `Authorization: Bearer`
   (§3/§6.10/§9). (2026-06-25)
8. **Context** — skills / prompts / artifacts only (tar→dir at hydration); no plugins. Prompts and
   artifacts live under `<home>/.ach-state/{prompts,artifacts}`; skills under
   `<home>/.config/opencode/skills`. (2026-06-30)
9. **`prompt.system` typed source** — `{type:text|file|ach}`; the bare string is rejected; file/ach
   resolve under `.ach-state` with load-time + read-time traversal rejection and missing-file
   hard-fail. (2026-06-30)
10. **`memory` discriminated union** — `type: hindsight` (default) | `codemem`; a legacy no-`type`
    block defaults to hindsight. (2026-07-01)
11. **schemaVersion = "1"** — both repos. (2026-06-25)
12. **Webhook acceptance decoupled from engine readiness** — the A′ cold-start gate (§6.7) is
    retired; acceptance depends only on harness readiness + `draining`. `202` accept (+ optional
    `X-ACH-Task-Id` correlation header/body); the engine starts lazily per `session_key`. A
    queryable task-status API and push callback are explicitly deferred. (2026-07-02)
13. **JSON Schema v1 frozen** — §2 is rendered to `docs/schemas/agent-config-v1.schema.json`
    (generated from `AgentConfig`, drift-guarded, published to gh-pages); authoritative for fields/types/defaults.
    Defaults aligned this cut: `idleTtlSeconds` 30, `maxInvocationSeconds` 600, `health.port` 8080,
    `OPENCODE_ENABLE_EXA` pinned on by the harness. Freezes the harness-accepted surface. (2026-07-02)
14. **`channel.session` becomes a block** — `key` (default `"none"`, BREAKING: was `"auto"`) /
    `maxTokens` / `overflow` (`compact` default | `rotate`); string shorthand `session: auto|none|"{{ … }}"`
    still accepted. Separates the router lane/pool key (`session_key`, untouched) from opencode
    conversation identity. See `docs/references/2026-07-02-session-identity-and-bounds.md`. (2026-07-02)

Implementation-level gates live in the GSD/superpowers plans (`docs/superpowers/plans/`), not here.
