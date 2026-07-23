# Design: Pi as a second engine (SP1 — egress-capable Pi engine)

**Status:** approved design, pre-plan
**Date:** 2026-07-23
**Author:** brainstorm w/ Juan Carlos
**Feeds:** `Skill(superpowers:writing-plans)` → implementation plan

---

## 1. Goal & context

Add **Pi** (`pi.dev` / `github.com/earendil-works/pi`, npm `@earendil-works/pi-coding-agent`,
Node/TS) as a second engine alongside the currently-hardcoded **opencode**. Pi fills the
`pymono` slot reserved in `docs/spec/ach-agent-runtime-spec-v1_4_2.md` §7.4. (The user's "py
agent" is Pi; the "pymono" label is the spec's engine-type name.)

**The router is not touched.** `router/lane.py` already calls the engine as an opaque injected
callable `engine_runner(event, on_kill)` and never imports `engine/` (D-08 / RTR-06). All
opencode specifics live in one closure, `main.py:685 engine_runner`, over four seams:
`EngineConfig` (opencode fields), `pool.acquire → ManagedServer`, `run_invocation`, and
`pool.oc_sessions`. SP1 abstracts exactly those.

**Non-goal:** SP1 does not change router invariants, lane ordering, or the three finite bounds.
The conformance suite must stay green throughout.

## 2. Decomposition

Two sub-projects; each gets its own spec → plan → implementation cycle.

- **SP1 (this doc) — a fully egress-capable Pi engine.** EngineDriver seam + Pi RPC driver +
  skills + model + MCP egress (which subsumes memory / repo-checkout / external-MCP /
  passthrough / codemem / a2a-egress). Exit criterion: **egress parity with opencode.**
- **SP2 — ops + contract + e2e.** Dockerfile (pin Pi + pi-mcp-adapter), cross-repo CONTRACT +
  `ach-runtime` operator `engine.type`, stats-mapping polish, broad e2e.

## 3. Key facts established (Pi's integration surface)

- **Transport:** Pi has **no** HTTP+SSE. Drive via `pi --mode rpc` = JSONL over stdin/stdout
  (strict LF framing; **not** `readline` — U+2028/9 hazard). Docs ship a Python client skeleton.
- **Skills:** Pi implements the **same Agent Skills standard** (SKILL.md + frontmatter) as
  opencode. Loads from `~/.agents/skills`, settings `skills:[]`, or `--skill`. The harness's
  hydrated skill tarballs drop in nearly unchanged.
- **Model + `ek_`:** declared purely in a config file — `$PI_CODING_AGENT_DIR/models.json`
  (`baseUrl`, `api`, dummy `apiKey`, `headers` with `$ENV` interpolation). **Zero TypeScript**
  for the model path. Point `baseUrl` at the localhost model proxy; the proxy injects `ek_`.
- **MCP:** Pi has **no native MCP**. Use vendored **pi-mcp-adapter**
  (`github.com/nicobailon/pi-mcp-adapter`, TS, MIT). Its `mcp.json` shape
  `{mcpServers:{name:{command/args/env | url/headers}}}` maps ~1:1 to ach-agent's `mcpServers`
  block and the harness's runtime `mcp_servers` list.
- **Isolation primitive:** `$PI_CODING_AGENT_DIR` relocates Pi's agent dir (settings, models.json,
  mcp.json, skills, caches) — the `OPENCODE_CONFIG` analog for the keyed engine pool.
- **a2a / memory / repo-checkout are localhost MCP servers** — they ride the one `mcp_servers`
  wiring, so MCP egress carries them.

## 4. Approach A — symmetric `EngineDriver`

Introduce an `EngineDriver` protocol; move today's opencode code behind an `engine/opencode/`
driver (behavior-preserving, guarded by the existing tests); add `engine/pi/`. `EnginePool`
becomes generic over the driver (its keyed-lock / TTL / `ManagedServer` logic — the part that
matters — is unchanged and shared).

### 4.1 Package layout

```
engine/
  base/
    driver.py     # EngineDriver protocol, TurnResult, EngineConfig (shared fields + engine_type)
    pool.py       # EnginePool — generic over a driver (keyed lock/TTL logic unchanged)
    server.py     # ManagedServer (generalized: process + opaque transport handle + session map)
    terminal.py   # NEW: harness-owned terminal-contract extract + Pydantic + <=1 repair
    context.py    # skills/prompts/artifacts hydration — extract-dir comes from the driver
    hydrate.py, sanitized_env.py, validator.py, stats mapping   # shared
  opencode/
    driver.py     # OpencodeDriver(EngineDriver): launch = write_opencode_config + build_opencode_env + serve
    client.py     # OpenCodeClient (HTTP/SSE) — moved
    events.py     # SSE -> tool/text/terminal events — moved
  pi/
    driver.py     # PiDriver(EngineDriver): launch = write models.json/settings.json/mcp.json + `pi --mode rpc`
    rpc.py        # NEW: JSONL stdin/stdout client (send command, iter events)
    events.py     # NEW: Pi event -> the same tool/text/terminal event shape
    mcp_json.py   # NEW: build mcp.json from the harness mcp_servers inputs (mirrors mcp_passthrough)
```

`a2a_egress.py`, `mcp_proxy.py`, `mcp_passthrough.py`, `repo_facade.py`, `repo_archive.py`,
`metrics.py` stay put.

### 4.2 The protocol

```python
class EngineDriver(Protocol):
    def skills_dir(self, home: Path, session_key: str) -> Path: ...   # where context.py extracts skills
    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer: ...
    async def health(self, server: ManagedServer) -> bool: ...
    async def run_turn(self, server: ManagedServer, *, conv_key: str, prompt: str, reuse: bool,
                       on_text, on_tool, max_tool_calls: int, stats: dict) -> str: ...
    async def stop(self, server: ManagedServer) -> None: ...
```

- `EnginePool.__init__(driver, sessions_map)`; `acquire`/`release`/TTL/`_stop` call
  `driver.launch`/`driver.health`/`driver.stop`. `pool.oc_sessions` → `pool.sessions` (rename).
- `_make_engine_runner` selects `OpencodeDriver()` vs `PiDriver()` by `cfg.engine.type`.
  **Everything else in that closure is unchanged** (memory wiring, prompt build, terminal-action
  selection, `session`/`reuse` decision, stats).

### 4.3 Terminal contract — **Fine boundary**

`run_turn` returns the raw final assistant text for **one** prompt. The harness-owned
`base/terminal.py` does text-extract + Pydantic + ≤1 repair by calling `run_turn` again with the
repair prompt. The structured-output contract lives **once**, engine-agnostic (matches the
"structured output is harness-validated" constraint). The opencode move carves this loop out of
`run_invocation`; the existing opencode tests catch regressions. `free_form` channels (`--tui`)
skip extraction, same as today.

## 5. Pi driver internals

### 5.1 Launch (`PiDriver.launch`)

No serve/port — the transport is the subprocess's stdin/stdout. Per `session_key`:

```
env:    PI_CODING_AGENT_DIR=<home>/pi/<key-suffix>          # isolation primitive
        + build_pi_env()   # clean-slate allowlist, same ek-hygiene as build_opencode_env
write:  $PI_CODING_AGENT_DIR/models.json    # localhost ek-proxy provider (dummy apiKey)
        $PI_CODING_AGENT_DIR/settings.json  # skills:[<dir>], defaultProjectTrust:"trust",
                                            # non-interactive (sampling:false, elicitation:false),
                                            # packages/extensions -> vendored pi-mcp-adapter
        $PI_CODING_AGENT_DIR/mcp.json       # section 6
spawn:  pi --mode rpc --provider <p> --model <id> --session-dir $PI_CODING_AGENT_DIR/sessions
```

`models.json` provider mapping reuses the `model_type` switch:
`openai→openai-completions /v1`, `gemini→google-generative-ai /gemini`,
`anthropic→anthropic-messages /anthropic`; each `baseUrl` = the localhost model proxy.
`defaultProjectTrust:"trust"` so Pi never blocks on the project-trust prompt headless.

Skills: `context.py` extracts hydrated tarballs to `driver.skills_dir(...)` and `settings.json`
references it — same reconcile-wipe + `capability.filter.exclude.skills` behavior as today.

### 5.2 RPC client (`pi/rpc.py`)

Small JSONL client: strict LF framing (strip trailing `\r`, never `readline`), `send(cmd_dict)`
to stdin, `async for event in events()` from stdout, request/response correlation via `id`.

### 5.3 The turn (`PiDriver.run_turn`)

1. **Session select** (mirrors opencode's `_sessions` map exactly): `reuse && conv_key known` →
   `switch_session` to its file iff the process was recreated (else already loaded);
   `reuse && new key` → record the fresh session file; `not reuse` → `new_session` first.
2. Send `{"type":"prompt","message":full_prompt}`.
3. Consume events: `text_delta` ⇒ `on_text`; `tool_execution_start/end` ⇒ `on_tool`; accumulate
   assistant text; stop at `agent_settled`.
4. Return raw final assistant text (Fine boundary; `terminal.py` validates/repairs).

### 5.4 Session model — **Durable**

Run Pi **with** `--session-dir`; keep `_sessions[conv_key] → session_file` on `ManagedServer`;
`switch_session` on relaunch. Matches opencode's disk-backed `channel.session: auto` continuity
across pool restarts (TTL expiry / crash). `auto|none|custom` behave identically on both engines.

### 5.5 Bounds & abort (invariants preserved)

- **`maxInvocationSeconds`** stays lane-owned: lane cancels the `run_turn` coroutine → driver
  sends `{"type":"abort"}` → pool force-releases (ttl=0 → `PiDriver.stop` = SIGTERM→SIGKILL the
  process group). Same shape as opencode's watchdog.
- **`max_tool_calls`**: Pi has no native bound → `rpc.py` counts `tool_execution_start` and sends
  `{"type":"abort"}` on exceed, then returns the terminal object. Enforced at the transport.
- **`on_kill`** → send `abort`.

## 6. MCP egress (the "a2a/MCP is a must" requirement)

a2a-egress, memory, repo-checkout, external proxied MCP, passthrough local/remote, and codemem
are **all localhost MCP servers**. MCP egress via pi-mcp-adapter carries them in one `mcp.json`.

`PiDriver.launch` writes `mcp.json` (via `pi/mcp_json.py`) from the same inputs opencode gets:

- `cfg.mcp_servers` (memory / repo / a2a facades) → **remote** entries whose `url` points at the
  **localhost proxy/facade** (the proxy/facade injects `ek_`; **no `${ACH_TOKEN}` in mcp.json** —
  identical hygiene to opencode.json today).
- `cfg.mcp_local_urls` (proxied external MCP via `McpProxy`) → **remote** entries at the localhost
  proxy.
- `cfg.extra_mcp_servers` (passthrough local/remote) → direct entries (reuse `mcp_passthrough`
  normalization).
- codemem → **local** stdio entry (`command: codemem mcp --db-path …`), mirroring opencode.

**Adapter provisioning:** pi-mcp-adapter is **vendored + pinned** into the image and referenced via
`settings.json` (`packages`/`extensions`) — **never** a runtime `pi install`. Supply-chain surface
is reviewed once at vendor time; `ek_` never reaches it (proxy-side injection).

**Tool semantics — `directTools: true` (decided).** Register named MCP tools in the system prompt
(parity with opencode's native tool names), with `excludeTools` from `capability.filter.exclude`.
Operator may drop specific large servers back to proxy mode. Headless settings: `sampling:false`
(or `samplingAutoApprove`), `elicitation:false`, no OAuth (localhost only).

**a2a facade hosting (shared prerequisite).** `build_a2a_mcp_server` is built-but-not-hosted today
(`main.py:1341`, VERIFICATION DEBT / Plan 3/4) — incomplete for opencode too. SP1 hosts the a2a
facade on a localhost port and adds it to the mcp block; **both engines** gain working a2a egress.

## 7. Config seam

`EngineBlock` gains `type: Literal["opencode","pi"] = "opencode"` (+ optional Pi sub-fields:
`binaryPath`, non-interactive overrides). `_make_engine_runner` picks the driver by
`cfg.engine.type`. CONTRACT + frozen schema (`docs/schemas/agent-config-v1.schema.json`)
regenerated. The `ach-runtime` operator side (`spec.engine.type`) lands in **SP2** (separate repo).

## 8. Security / `ek_` hygiene (unchanged invariants)

- `ek_` (ACH_TOKEN/ACH_API_KEY) NEVER logged, NEVER written to `models.json` / `settings.json` /
  `mcp.json`, NEVER forwarded into the Pi subprocess env. `build_pi_env()` is clean-slate allowlist
  (mirror of `build_opencode_env`), and `forward_env` never lists the ek.
- Model + MCP both reach ACH only through the localhost proxy/facades, which inject the ek
  harness-side. Pi and pi-mcp-adapter see only loopback URLs + dummy credentials.
- Pi has no permission gate and runs with the launching user's perms — fine, because the harness
  already sandboxes the engine (clean-slate env + `dumpable=0`), same as opencode.

## 9. Observability / stats

Pi emits richer JSONL events (`tool_execution_*`, per-message usage/cost). `pi/events.py` maps
them to the **same** stat contract (`ach:sessions`, `ach:tools`, `v="1"`) the SSE path feeds today
— different event shape, identical sink. Observability never breaks a turn (swallow-and-continue).

## 10. Test strategy

- **Opencode move is behavior-preserving** — existing `tests/engine/` + `make conformance` are the
  safety net; must stay green.
- **Pi driver units** against a fake `pi` subprocess replaying JSONL fixtures: happy turn, tool
  loop, `max_tool_calls` abort, session switch/reuse, timeout/abort, terminal extract+repair.
- **`mcp.json` generation** tests mirroring `test_mcp_passthrough.py` (facade/proxy → remote at
  loopback; codemem → local; passthrough; excludeTools).
- **`models.json` generation** tests (provider mapping per `model_type`; dummy apiKey; no ek).
- **One e2e** with a real `pi` + a stub MCP server behind the localhost proxy; assert `ek_` never
  appears in `models.json` / `settings.json` / `mcp.json` / the Pi subprocess env.

## 11. Scope fence

**In SP1:** EngineDriver seam (A) + opencode move; Pi driver (launch, RPC, run_turn, durable
sessions, bounds/abort); skills; model via models.json; MCP egress via vendored pi-mcp-adapter
(memory / repo / external / passthrough / codemem / a2a); a2a facade hosting; `engine.type` schema
+ CONTRACT; tests above.

**Deferred to SP2:** Dockerfile (pin Pi + adapter), `ach-runtime` operator `engine.type`,
stats-mapping polish beyond parity, broad e2e matrix.

## 12. Risks / open items

- pi-mcp-adapter is single-maintainer npm — mitigate by vendoring + pinning + one-time review; ek
  never reaches it.
- `directTools` first-run falls back to proxy until the metadata cache populates — acceptable;
  document; `/mcp reconnect` forces it (n/a headless — cache warms in background).
- a2a facade hosting is net-new shared work (was VERIFICATION DEBT) — verify it against opencode
  first so Pi inherits a proven facade.
- Carving the terminal-repair loop out of `run_invocation` touches IP-adjacent code — rely on the
  opencode test suite as the regression gate.

## 13. References

- Pi docs: `packages/coding-agent/docs/{rpc,json,skills,models,custom-provider,extensions,settings}.md`
- pi-mcp-adapter: `github.com/nicobailon/pi-mcp-adapter` (README: mcp.json, directTools, remote/headers)
- ach-agent: `main.py:685` (engine_runner), `engine/lifecycle.py` (EngineConfig, run_invocation,
  write_opencode_config, build_opencode_env), `engine/pool.py` (EnginePool), `engine/context.py`
  (skills), `engine/a2a_egress.py`, `docs/superpowers/plans/2026-07-07-mcpservers-block.md`
- Runtime spec engine types: `docs/spec/ach-agent-runtime-spec-v1_4_2.md` §7.4
