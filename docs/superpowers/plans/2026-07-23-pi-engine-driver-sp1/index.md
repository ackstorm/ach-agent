# Pi Engine Driver (SP1) — Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan **one phase document at a time, in order**. Every phase doc uses checkbox (`- [ ]`) steps for tracking. This index is the map; the per-phase docs are the plan.

**Source spec:** [`docs/superpowers/specs/2026-07-23-pi-engine-driver-sp1-design.md`](../../specs/2026-07-23-pi-engine-driver-sp1-design.md)

**Goal:** Add **Pi** (`pi.dev` / `github.com/earendil-works/pi`, npm `@earendil-works/pi-coding-agent`) as a second, fully **egress-capable** engine alongside opencode, behind a symmetric `EngineDriver` seam — without touching the router or its invariants.

**Architecture:** Introduce an `EngineDriver` protocol in `engine/base/`; relocate today's opencode code behind an `engine/opencode/` driver (behavior-preserving, guarded by the existing tests); add `engine/pi/`. `EnginePool` becomes generic over the driver (its keyed-lock / TTL / `ManagedServer` logic is unchanged and shared). The harness-owned terminal contract (text-extract + Pydantic + ≤1 repair + step-budget wrap-up) is carved into `engine/base/terminal.py` and driven engine-agnostically off a `TurnResult`. Pi is driven via `pi --mode rpc` (JSONL over stdin/stdout — **no HTTP/SSE**); its model reaches ACH through the same localhost model-proxy, and its MCP egress rides a vendored **pi-mcp-adapter** whose `mcp.json` points at the same localhost proxy/facades. `ek_` never reaches Pi or the adapter.

**Tech stack:** Python 3.12 + asyncio, Pydantic v2, structlog, uvicorn (facade hosting), `pi --mode rpc` (Node/TS subprocess), pi-mcp-adapter (vendored TS), uv / ruff / mypy --strict / pytest(asyncio_mode=auto).

---

## Global Constraints

Every task in every phase implicitly includes these. Values are copied verbatim from the spec / repo conventions.

- **The router is not touched.** `router/lane.py` calls the engine as the opaque injected `engine_runner(event, on_kill)`; it never imports `engine/` logic except the `ENGINE_WATCHDOG_KILLS` counter (`lane.py:27`). SP1 changes **nothing** in `router/`. The conformance suite (`make conformance`, 11 named invariants) MUST stay green at every commit.
- **Three finite bounds unchanged:** `maxConcurrentInvocations`, `maxInvocationSeconds` (lane-owned, RTR-04), `maxQueuedTotal`. `max_tool_calls` stays engine-enforced at the transport.
- **`ek_` hygiene (never regress):** `ek_` (`ACH_TOKEN`/`ACH_API_KEY`) is NEVER logged, NEVER written to any engine config file (`opencode.json`, Pi `models.json` / `settings.json` / `mcp.json`), NEVER forwarded into the engine subprocess env. Model + MCP reach ACH only through the localhost proxy/facades, which inject the `ek_` harness-side. The engine sees only loopback URLs + dummy credentials. Subprocess env is clean-slate allowlist (`build_opencode_env` / new `build_pi_env`).
- **Structured output is harness-validated once**, engine-agnostic: text-extract + Pydantic + ≤1 repair + step-budget wrap-up live in **one** place (`engine/base/terminal.py`). `free_form` channels (`--tui`) skip extraction.
- **Observability never breaks a turn:** stat/metric sinks swallow their own exceptions; the Redis-stream contract (`ach:sessions`, `ach:tools`, `v="1"`) is unchanged. Pi maps its native events to the **same** sink shape.
- **Canonical engine wire name is `pi`.** The runtime spec §7.4 reserves `pymono` for this slot — amend it to `pi` (no alias). `EngineBlock.type: Literal["opencode","pi"] = "opencode"`.
- **Pi transport specifics (fact-checked, do not deviate):** strict **LF framing** (strip trailing `\r`; **never** `readline` — U+2028/U+2029 hazard); terminal event is **`agent_settled`**, NOT `agent_end`; text lives in `message_update` → nested `assistantMessageEvent` with `type == "text_delta"` (must unwrap); `defaultProjectTrust` valid values are `ask|always|never` → use **`"always"`** (there is no `"trust"`); a prompt sent mid-stream errors unless `streamingBehavior` is set — the lane serializes turns and repair/wrap sends wait for `agent_settled`, so this never fires.
- **Sessions map is pool-owned and namespaced by engine type.** Never rename the on-disk SQLite table (`oc_sessions` stays — it is persisted state). Map values are engine-native refs (opencode `ses_…` id; Pi session-file path); a transparent per-engine-type key prefix prevents an opencode id from ever being fed to Pi's `switch_session` as a path on a persisted home whose `engine.type` flipped.
- **Skills hydration runs ONCE at boot** (`main.py:1240`), before any `session_key` exists — `driver.skills_dir(home)` takes **no** `session_key`; all per-key configs point at the one shared dir.
- **pi-mcp-adapter is vendored + pinned** into the image and referenced via `settings.json` `packages` — **never** a runtime `pi install`. (Dockerfile pinning itself is SP2; SP1 references the vendored path.)
- **venv/uv only** (never system-wide `pip install`); `make lint` = `ruff check` + `ruff format --check` + `mypy --strict`; multi-stage Docker with explicit `COPY` (SP2).

---

## Shared interface contract (canonical signatures)

These are defined in **Phase 1** and consumed by all later phases. Each phase doc restates the exact slice it consumes/produces in its own **Interfaces** block; this is the single source of truth if they ever disagree.

```python
# engine/base/driver.py
@dataclass
class TurnResult:
    text: str                                   # raw final assistant text for ONE prompt
    session_ref: str                            # engine-native ref the turn ran in
                                                #   (opencode: ses_… id; pi: session-file path)
    aborted: bool = False                       # step-budget cut this turn (usually no terminal object)

class EngineDriver(Protocol):
    engine_type: str                            # "opencode" | "pi" — used to namespace the sessions map

    def skills_dir(self, home: Path) -> Path: ...          # SHARED extract dir; NO session_key

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer: ...
    async def health(self, server: ManagedServer) -> bool: ...

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],     # pool-owned map (already engine-namespaced)
        session_ref: str | None = None,         # continue EXACTLY this session (repair/wrap-up);
                                                #   bypasses conv_key/reuse and the map
        on_text: Callable[[str], None] | None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None,
        max_tool_calls: int,
        stats: dict[str, Any],
    ) -> TurnResult: ...

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None: ...  # 'none' / rotate
    async def compact_session(self, server: ManagedServer, session_ref: str) -> None: ...  # overflow=compact
    async def stop(self, server: ManagedServer) -> None: ...
```

```python
# engine/base/terminal.py — engine-agnostic terminal contract
async def run_contract_turn(
    driver: EngineDriver,
    server: ManagedServer,
    *,
    conv_key: str,
    prompt: str,
    reuse: bool,
    sessions: MutableMapping[str, str],
    free_form: bool,
    terminal_action: str,        # "none" | "a2a_reply"
    terminal_retries: int,
    on_text, on_tool,
    max_tool_calls: int,
    stats: dict[str, Any],       # run_turn writes stats["session_ref"] (+ opencode: stats["oc_session_id"])
) -> dict[str, Any]:             # the terminal object {"action": ..., "text": ...}
    ...
```

`run_contract_turn` calls `driver.run_turn` for the first prompt; on `TurnResult.aborted` it runs ONE wrap-up turn (`max_tool_calls=0`, same `session_ref`); on missing terminal object it runs ≤1 repair turn (same `session_ref`). `stats["session_ref"]` carries the final ref for post-turn hygiene.

`EngineConfig` (relocated to `engine/base/driver.py`) keeps all current fields and gains `engine_type: str = "opencode"` (Phase 1); Phase 8 additionally adds `pi_mcp_adapter_path: str = ""`. All existing fields (`model`, `model_type`, `params`, `model_base_url`, `mcp_servers`, `mcp_local_urls`, `extra_mcp_servers`, `codemem_*`, `system_prompt`, `compose`, `steps`, `forward_env`, `exclude_tools`, `binary_path`, `home`, `work_dir`, …) are already engine-agnostic and reused as-is.

---

## Phases (execute in this order)

| # | Document | Delivers | Exit criterion |
|---|----------|----------|----------------|
| 1 | [phase-1-seam-foundation.md](phase-1-seam-foundation.md) | `engine/base/` package: `EngineDriver` protocol, `TurnResult`, `EngineConfig` (relocated + `engine_type`) | Additive; all existing tests + `make conformance` green; new type/protocol tests pass |
| 2 | [phase-2-opencode-relocation.md](phase-2-opencode-relocation.md) | Physical moves `client.py`→`opencode/client.py`; `events.py` split (shared vocab→`base/events.py`, SSE parser→`opencode/events.py`); re-export shims + updated `patch()` targets | Whole suite green with **zero behavior change** |
| 3 | [phase-3-terminal-and-opencode-driver.md](phase-3-terminal-and-opencode-driver.md) | `engine/base/terminal.py` (carved-out contract loop) + `engine/opencode/driver.py` `OpencodeDriver` implementing the protocol (`run_turn`→`TurnResult`) | Opencode terminal/repair/wrap-up tests + new driver-unit tests green |
| 4 | [phase-4-enginepool-generic.md](phase-4-enginepool-generic.md) | `engine/base/pool.py` `EnginePool(driver, sessions_map)`, transparent engine-type namespacing, `oc_sessions`→`sessions` rename, shim `engine/pool.py` | `test_pool` + all pool consumers green |
| 5 | [phase-5-config-seam-schema.md](phase-5-config-seam-schema.md) | `EngineBlock.type` (+ optional Pi sub-fields), `make schema` regen, runtime-spec §7.4 `pymono`→`pi`, CONTRACT note | Schema drift-guard (`test_schema_artifact`) green; `type: pi` accepted |
| 6 | [phase-6-engine-runner-rewire.md](phase-6-engine-runner-rewire.md) | `main.py` `_make_engine_runner` selects the driver by `cfg.engine.type`, calls `run_contract_turn`, hygiene via `driver.discard_session`/`compact_session` keyed on `session_ref` | `make conformance` + integration green; opencode identical end-to-end |
| 7 | [phase-7-a2a-facade-hosting.md](phase-7-a2a-facade-hosting.md) | `A2AEgressFacade` hosts `build_a2a_mcp_server` on loopback (was VERIFICATION DEBT); wired into boot + the shared `mcp_servers` list for **both** engines | a2a egress works for opencode (proven), unit + wiring tests green |
| 8 | [phase-8-pi-driver.md](phase-8-pi-driver.md) | `engine/pi/`: `rpc.py`, `events.py`, `models_json.py`, `mcp_json.py`, settings writer, `PiDriver` (launch, run_turn, durable sessions, bounds/abort), `build_pi_env` | Pi driver units against fake `pi` + one real-`pi` e2e green; **egress parity with opencode** |

**Dependency chain:** 1 → 2 → 3 → 4 → 5 → 6, then 7 (independent of Pi; do before 8 so Pi inherits a proven a2a facade), then 8. Phases 5 and 7 are the only ones that could be reordered, but the table order is the recommended path.

---

## Spec traceability

| Spec section | Covered by |
|--------------|-----------|
| §4.1 package layout | Phases 1–4, 8 |
| §4.2 `EngineDriver` protocol + `EnginePool` generic + `oc_sessions`→`sessions` | Phases 1, 4 |
| §4.3 terminal contract (Fine boundary) | Phase 3 |
| §5.1 Pi launch (models.json/settings.json/mcp.json + `pi --mode rpc`) | Phase 8 |
| §5.2 RPC client (LF framing) | Phase 8 |
| §5.3 the turn (`assistantMessageEvent` unwrap, `agent_settled`) | Phase 8 |
| §5.4 durable sessions + engine-type namespacing | Phases 4, 8 |
| §5.5 bounds & abort | Phase 8 |
| §6 MCP egress (mcp.json, directTools, vendored adapter) | Phase 8 |
| §6 a2a facade hosting (shared prerequisite) | Phase 7 |
| §7 config seam (`engine.type` + schema + runtime spec) | Phase 5 |
| §8 security / `ek_` hygiene | Global constraints, enforced in Phases 6, 8 |
| §9 observability / stats | Phase 8 (Pi event → same sink) |
| §10 test strategy | Phases 3, 6, 7, 8 |

**Deferred to SP2 (out of scope here):** Dockerfile pinning Pi + adapter, `ach-runtime` operator `spec.engine.type`, stats-mapping polish beyond parity, broad e2e matrix. (Spec §11.)

---

## Decision record

On completion, add one row to [`docs/references/README.md`](../../../references/README.md) pointing at a new decision write-up `docs/references/2026-07-DD-pi-engine-driver.md` summarizing the `EngineDriver` seam. (Task in Phase 8.)

## Execution handoff

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per **task** (not per phase), review between tasks, fast iteration. Use `superpowers:subagent-driven-development`.
2. **Inline Execution** — execute phase-by-phase in this session with a checkpoint at each phase boundary. Use `superpowers:executing-plans`.

Whichever is chosen, honor the phase order above and keep `make conformance` green at every commit.
