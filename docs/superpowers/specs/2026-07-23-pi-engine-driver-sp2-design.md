# Design: Pi second engine (SP2 — ops, contract & e2e)

**Status:** draft design, pre-plan (feeds `Skill(superpowers:writing-plans)`)
**Date:** 2026-07-23
**Author:** brainstorm w/ Juan Carlos
**Depends on:** SP1 (`2026-07-23-pi-engine-driver-sp1-design.md`) — the EngineDriver seam +
Pi RPC driver + harness `engine.type` config. SP1 delivers the *code*; SP2 makes it *deployable*.

> **Scope note:** `../ach-runtime` (the `AgentRuntime` CRD) is **out of SP2 scope** — the
> operator coordination is only with `../ach` (the `AgentProfile` that renders the harness
> config). If the runtime CRD ever needs `engine.type: pi`, that is a separate `../ach-runtime`
> repo cycle, not this plan.

---

## 1. Goal & context

SP1 built a fully egress-capable Pi engine and the `engine.type: opencode|pi` harness config
seam (`config/schema.py:61` `PiEngineBlock{binaryPath, mcpAdapterPath}`, `engine/pi/driver.py`).
But nothing yet **ships** Pi: the runtime image has no `pi` binary, the `../ach` operator can't
render `engine.type: pi`, and Pi has no e2e that actually boots the binary. SP2
closes exactly that gap — **ops + contract + e2e** — so a control plane can render
`engine.type: pi` and get a working agente.

**Non-goal:** SP2 adds **no new engine behavior and no new IP**. Router invariants, lane
ordering, and the three finite bounds are untouched. No new engine *types* beyond `pi`
(`claudeCode`/`codex`/… stay reserved in runtime-spec §7.4).

## 2. Scope — two repos

SP2 straddles two repos. The Python repo I execute directly; the `../ach` Go change is packaged
as a **detailed handoff prompt** for the ACH operator agent.

| # | Item | Repo | Execution |
|---|------|------|-----------|
| 1 | Dockerfile `pi-bin` stage (pin Pi + pi-mcp-adapter) | **ach-agent** | here |
| 2 | CONTRACT_v3 `engine.pi` cross-repo contract polish | **ach-agent** | here |
| 3 | Stats-mapping polish beyond parity (Pi turn → `TurnStat`) | **ach-agent** | here |
| 4 | Broad e2e matrix (image-boot Pi turn + MCP + durability + a2a) | **ach-agent** | here |
| 5 | `AgentProfile.engine` renders `type`/`pi` into the harness config | **../ach** | handoff prompt |

**Cross-repo release ordering (hard constraint):** the ach-agent **image must ship `pi`
before** `../ach` advertises `engine.type: pi` — otherwise a rendered config references a binary
the image lacks. Item 1 (+ a released image tag) is the gate for item 5.

## 3. Key facts established

- **Harness config is already done (SP1).** `PiEngineBlock{binary_path="pi",
  mcp_adapter_path=""}` (`config/schema.py:61-72`); empty `mcpAdapterPath` → driver default
  `/opt/pi-mcp-adapter` (`engine/pi/driver.py:39`). SP2 only has to make those paths *exist in
  the image* and *reachable from the CRDs*.
- **Pi + pi-mcp-adapter are npm packages** (`@earendil-works/pi-coding-agent`,
  `pi-mcp-adapter`). The runtime image **already carries Node 26** (the `codemem-bin` stage,
  `Dockerfile:17`, `COPY node` at `:69`) — Pi reuses that one `node` binary; no second runtime.
- **Dockerfile stages today:** `opencode-bin` → `codemem-bin` → `builder` → runtime
  (`Dockerfile:1-104`). The `codemem-bin` stage is the exact template for a Pi npm stage
  (npm `--prefix` install + one COPY + a `--version` runtime smoke).
- **e2e already exists but skips.** `tests/e2e/test_pi_e2e.py` skips unless `shutil.which("pi")`
  and an adapter dir resolve (`:18-20,73-80`); it already asserts `ACH_API_KEY` absent from the
  Pi subprocess env (`:120`). SP2 makes the binary+adapter present so the skip lifts in CI.
- **Operator seam = `../ach` only.** `api/ach/v1alpha1/agentprofile_types.go:41-58` —
  `EngineSpec` is the harness-local `engine.*` block (**no `type` field yet**) that renders into
  CONTRACT_v3's `engine`. This is the one operator surface SP2 touches. (`../ach-runtime`'s
  `AgentRuntime.engine.type` enum is deliberately out of scope, see the scope note above.)
- **Stats.** `stats/models.py:TurnStat{input_tokens, output_tokens, cost, tokens_per_s}`;
  SP1 mapped Pi assistant usage (commit `389126e`). "Beyond parity" = don't leave `cost`/
  `tokens_per_s` at zero for a Pi turn if Pi reports usage.

## 4. Dockerfile — `pi-bin` stage (item 1)

Mirror `codemem-bin`. New stage installs both npm packages into an isolated prefix, pins both
versions as `ARG`s, and the runtime stage COPYs the prefix + puts `pi` on PATH and the adapter
at `/opt/pi-mcp-adapter` (the driver default). Build-time smoke: `pi --version` **and** assert
the adapter dir is non-empty — closes the "present-but-broken" half of fail-open, same as
codemem's `codemem --version` (`Dockerfile:76`).

```dockerfile
# ── pi stage ───────────────────────────────────────────────────────────────
FROM node:26-bookworm-slim AS pi-bin
ARG PI_VERSION=<PIN>
ARG PI_MCP_ADAPTER_VERSION=<PIN>
RUN npm install -g --prefix /opt/pi "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm install --prefix /opt/pi-mcp-adapter "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}"
# runtime stage additions:
COPY --from=pi-bin /opt/pi /opt/pi
COPY --from=pi-bin /opt/pi-mcp-adapter /opt/pi-mcp-adapter
ENV PATH="/opt/pi/bin:${PATH}"
RUN pi --version && test -n "$(ls -A /opt/pi-mcp-adapter)"
```

Exact prefix layout / whether the adapter is a `packages:[path]` dir vs an installed
`node_modules` subtree is pinned at plan time against the real npm tarballs (SP1's
`build_pi_settings(mcp_adapter_path)` consumes it, `engine/pi/config.py:17-22`).

## 5. CONTRACT_v3 — cross-repo contract polish (item 2)

CONTRACT_v3 already carries `engine.type: opencode|pi` + `engine.pi` (`:121-123`). SP2 makes it
a *complete* cross-repo contract: document `engine.pi.{binaryPath, mcpAdapterPath}` as the
rendered seam, state that **`../ach` renders it** (the harness validates it), and record the
image-ships-before-operator-advertises ordering rule (§2). Pure docs; no schema change (SP1
froze the schema).

## 6. Stats-mapping polish (item 3)

Add a unit test that a Pi turn produces a fully-populated `TurnStat` (tokens **and** `cost`
**and** `tokens_per_s`, correct `model` label). If SP1 left `cost=0` for Pi, close it to match
opencode's source (see open decision D-3). One test, one mapping fix if needed — not a rewrite.

## 7. e2e matrix (item 4)

The "broad e2e" is the **engine-parametrized** set, plus one image-boot proof:

- **Image-boot Pi turn** — build the runtime image, `docker run` it with a stub ek + ACH
  endpoint, drive one Pi turn, assert a reply. This is the authority that packaging works
  (SP1 §10 precedent: the real binary is the source of truth, unit tests use fixtures).
- Lift the skips on `test_pi_e2e.py` in CI (binary now present).
- Pi counterparts of the existing opencode e2e where they carry engine-specific risk: MCP
  structured egress (`test_opencode_mcp_structured_e2e.py`), durability
  (`test_durability_e2e.py`), a2a (`test_a2a_e2e.py`). Parametrize by engine rather than
  copy-paste where the harness path is identical.
- **Hygiene assertion stays concrete:** ek (`ACH_*`/`ek_`) absent from the Pi subprocess env,
  `models.json`, `mcp.json`, and the adapter (already asserted in `test_pi_e2e.py:120`).

## 8. Operator handoff — `../ach` (item 5, prompt)

`api/ach/v1alpha1/agentprofile_types.go` `EngineSpec` (`:41`):
- Add `Type string` (`+optional`; omitted → harness default) and
  `Pi *PiEngineSpec{BinaryPath, McpAdapterPath string}`.
- Render both into the harness `engine` block (the render path that produces CONTRACT_v3's
  `engine.type`/`engine.pi`).
- Regen CRD. Add a render test asserting `engine.type: pi` + `engine.pi.*` reach the config.

## 9. Security / `ek_` hygiene (unchanged)

Every SP1 invariant carries over verbatim: the ek never appears in Pi's env, `models.json`,
`settings.json`, `mcp.json`, or the pi-mcp-adapter. SP2 adds only build-time packaging and CRD
fields — none of which carry secrets (CRDs carry env **names**, never values).

## 10. Scope fence

**In SP2:** Dockerfile `pi-bin` stage (pinned); CONTRACT cross-repo polish; stats-mapping polish
+ its test; image-boot + parametrized e2e; `../ach` `engine.type`/`pi` render (delivered as a
handoff prompt).

**Out:** `../ach-runtime` `AgentRuntime.engine.type` (separate repo cycle); new engine types
beyond `pi`; Helm chart pi presets beyond the CRD field; Pi performance tuning; any router /
lane / bounds change.

## 11. Risks / open items

- **pi-mcp-adapter is single-maintainer npm** (carried from SP1 §12): pin + one-time review; ek
  never reaches it. Vendoring mechanism = open decision D-1.
- **Pi × adapter version compat:** pin both `ARG`s; build-time `pi --version` + adapter-dir
  non-empty smoke catches a bad pin. A field-name drift is a one-line fix (SP1 isolated Pi wire
  literals in `pi/protocol.py`).
- **Image size:** the `pi-bin` npm tree adds to the (already Node-carrying) image; both engines
  share the single `node` binary, so the delta is the JS packages only. Accepted.
- **Cross-repo release ordering** (§2): image-with-pi must be released before `../ach` advertises
  `engine.type: pi`. The handoff prompt states this as a precondition.

## 12. Open decisions (need your call before the plan)

- **D-1 — adapter vendoring:** npm-install-pinned in the Dockerfile (recommend — matches the
  `codemem-bin` pattern exactly, no repo bloat) **vs** git-vendor the adapter into the repo.
- **D-2 — `../ach` `EngineSpec.Type`:** free `string` (harness is the enforcer, mirrors the
  block's "unset → harness default" style) **vs** enum-lock to `opencode;pi` (duplicates the
  runtime CRD's validation earlier). Recommend **free string**.
- **D-3 — Pi `cost` source:** compute from `tokens × price-table` **vs** Pi-reported usage.
  Recommend **whatever opencode already does** (strict parity — check its mapping at plan time).
- **D-4 — e2e in CI:** run the image-boot Pi e2e in CI (CI image carries Node-pi) as the
  authority **vs** keep Pi e2e skip-guarded / local-only. Recommend **image-boot in CI** (SP1
  §10 "real binary is the authority" precedent).

## 13. References

- SP1: `docs/superpowers/specs/2026-07-23-pi-engine-driver-sp1-design.md`, plan folder
  `docs/superpowers/plans/2026-07-23-pi-engine-driver-sp1/`
- ach-agent: `Dockerfile` (codemem-bin template `:17-24,69-76`), `config/schema.py:61`,
  `engine/pi/{driver,config}.py`, `tests/e2e/test_pi_e2e.py`, `stats/models.py`,
  `docs/plan/CONTRACT_v3.md:121-123`
- `../ach`: `api/ach/v1alpha1/agentprofile_types.go:41-58` (+ render path under
  `internal/platformapi/render`)
- Pi docs / pi-mcp-adapter: see SP1 §13
