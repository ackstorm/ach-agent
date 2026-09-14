# ACH Agent — Startup Hydration and Storage Boundary

**Status:** Implemented and locally validated on `feat/operator-storage`, 2026-09-14. Release and joint Kubernetes operator e2e remain pending.
**Baseline:** ACH Agent main `1b240ef`, containing the three-role split.
**Scope:** Replace permanent shared hydration links with a startup download/install handoff. Keep the existing channels, execution, session and proxy behavior.
**Audience:** ACH Agent and ACH operator engineers; access to agent source is not required.

## 1. Responsibilities

ACH Agent has three roles:

- **Channels:** receives source events and delivers outcomes.
- **Harness:** owns private agent configuration, credentials, admission, prompts, proxies and channel hooks.
- **Mini-harness:** lives in the engine container; installs engine inputs and manages native Pi/OpenCode processes and sessions.

The word **hydration** covers two distinct phases:

1. **Download**, performed by Harness using its ACH credentials.
2. **Install**, performed by the mini-harness inside the engine container.

Native engine configuration belongs in engine HOME. The download directory is only a temporary delivery location.

## 2. Storage: four purposes

| Storage | Harness access | Engine-container access | Lifetime |
| --- | --- | --- | --- |
| Harness-private state | Read/write | None | Existing persistence policy |
| Engine HOME | None | Read/write | Existing persistence policy |
| Workspace | Read/write | Read/write | Existing workspace/session lifecycle |
| Hydration transfer directory | Read/write | Read/write | Temporary; one download batch per init attempt |

Channels has none of these mounts. Each container retains its private writable `/tmp`.

For distributed mode, use these generic mount paths:

```text
base/state       Harness-private
base/home        Engine-private
base/workspace   Shared workspaces

/run/ach-agent/transfer   Shared temporary transfer mount
```

`base` is `persistence.mountPath` for persistent storage, or `/tmp/ach-agent` for nonpersistent storage. Persistent backing can use three subpaths of one PVC. Temporary backing can use `emptyDir` with the same access boundaries.

The transfer mount is always temporary, for example its own `emptyDir`. Harness creates a batch beneath it:

```text
/run/ach-agent/transfer/
└── .ach-harness-shared-files-<random>/
    ├── prompts/
    ├── artifacts/
    └── skills/
```

Use `mkdtemp` **inside the shared transfer mount**, not inside Harness's private `/tmp`. Both containers see the same absolute batch path.

The operator treats these directories as opaque. It must not know codemem, native configuration filenames, skill discovery paths or the contents of hydration.

## 3. Startup sequence

Initialization is part of environment startup. **It must not wait for a channel event, invocation or native session acquisition.**

```mermaid
sequenceDiagram
    participant H as Harness
    participant M as Mini-harness
    participant N as Pi / OpenCode
    M->>M: Open control socket; not initialized
    H->>H: Read private config, contact ACH, start proxies
    H->>H: Download files into a fresh shared batch
    H->>H: Resolve any prompt text needed by Harness
    H->>M: Existing controller-open/init with public config + batch path
    M->>M: Copy files into engine HOME
    M->>M: Generate and validate native configuration
    M->>M: Delete the consumed batch
    M-->>H: Initialization complete
    Note over H,M: Startup/health/readiness now succeed
    H->>M: Execute admitted work later
    M->>N: Launch or reuse native engine
```

The native agent does not run a model turn during init. Startup checks may verify the selected executable, required integration files, writable native directories and generated configuration without performing user work.

Per-session configuration that depends on a workspace, trace token or conversation still belongs to native launch. Init installs and validates the boot-static inputs; it does not invent a dummy invocation or native conversation.

## 4. Handoff contract

Reuse the existing controller-open/configuration request over `agent.sock`. Do not introduce a second transport or an independent bootstrap service.

Its initialization inputs are:

- The allowlisted public engine configuration: selected engine, model/proxy/MCP settings, eligible environment names and execution settings. No environment values cross this request.
- One absolute path to the completed download batch, conceptually `hydrationDir`.

This is an **internal wire field**, not a new AgentProfile or agent YAML setting. Do not send the complete private agent configuration or managed upstream credentials.

Harness completes all downloads and its own reads of that batch before handing it over. After sending init, Harness stops modifying or removing it. The mini-harness owns installation and disposal.

The response means **installation, configuration checks and batch deletion have completed successfully**. A socket accepting connections does not imply successful initialization.

The batch path must identify a batch inside the agreed transfer mount. Copy/delete operations must not traverse into private storage or remove the mount root. These are transfer-operation constraints, not inspection of operator scripts or Git repositories.

## 5. Native installation

The mini-harness installs files in the locations its native adapter already uses:

- Skills in the selected engine's skill discovery directory.
- Prompts and artifacts in engine-owned installed context, accessible through the existing `.ach-state` convention.
- Model/MCP/native integration configuration in engine HOME.

These are real installed files. **No permanent symlink may point into the disposable transfer batch.**

Workspace `.ach-state` links may point to installed engine-owned context. Harness must not rely on following those links to read E-private HOME; it already resolved any prompt content it needs before handoff. Prepare retains workspace access but is not promised direct access to private engine files.

Refresh only the directories managed by ACH hydration. Preserve native sessions, conversation mappings, caches and unrelated files in engine HOME. Removed/excluded managed skills must not remain installed from an older startup.

Once installation and validation succeed, the mini-harness deletes **that batch**, not the workspace, other batches or the whole transfer mount.

## 6. Health and failure

| State | Accept init/control discovery | Accept execution | Startup/health/readiness |
| --- | --- | --- | --- |
| Waiting for init | Yes | No | Not successful |
| Installing/validating/deleting | Control as needed | No | Not successful |
| Initialized | Existing controller rules | Yes | Successful, subject to existing health conditions |
| Init failed | No further execution | No | Failed; mini-harness exits nonzero |

Control discovery must be reachable before health is successful. The local launcher and Harness must not wait for `/readyz` or successful startup health before sending init, which would deadlock initialization.

Startup probes provide the initialization allowance. Liveness enforcement must not kill a healthy installation merely because init has not finished yet. Waiting for Harness and installation remain bounded by startup policy; there is no indefinite pre-init state.

A download failure prevents handoff. An installation, validation or disposal failure prevents readiness and causes the mini-harness to exit nonzero, making the engine container fail startup. Native execution must never start from partially installed inputs.

A later Pi/OpenCode launch failure during an invocation retains the existing per-invocation `LaunchFailed` behavior. Do not turn every native launch failure into a container init failure.

## 7. Restarts and ownership

Init runs once per successful engine-environment startup, not once per event or conversation.

Use existing controller ownership and instance identity. Do not add a lease, heartbeat or separate generation protocol.

If the engine container restarts, its new mini-harness instance requires initialization before readiness. Harness must supply a fresh download batch: the previous successful batch has been deleted. Installed persistent native data remains available and must not be reset.

If a connection fails during init, do not infer either completion or failure from the missing response alone. Existing controller/instance handling must establish whether the same instance completed initialization or a replacement needs a fresh attempt. Never replay a user prompt as part of recovering init.

Failed/abandoned transfer batches are temporary startup data. They may be removed as part of bounded startup recovery; they are not channel workspace cleanup. No background artifact service is required.

## 8. Channel prepare and cleanup are independent

For an admitted event:

1. Harness selects the existing workspace by `session_key`.
2. Harness runs the configured prepare there, using its private hook HOME and configured environment.
3. Harness asks the already initialized mini-harness to execute the work.
4. Channel cleanup runs at its existing lifecycle point, after required native stop confirmation.

Prepare output is written to the workspace, not the hydration batch. The mini-harness deleting a hydration batch cannot delete prepared work.

Keep warm reuse, FIFO, conversation-key behavior, terminal repair, usage, streaming, deadlines and cancellation. ACH does not inspect Git commands, rewrite checkout configuration or impose artifact publishing on channel hooks.

## 9. Operator contract

Keep `AgentProfile.spec.placement`, profile-only, `standalone | distributed`, default `standalone`.

- Standalone: one combined container, no role arguments; the parent-owned mini-harness performs the same startup install before work.
- Distributed: one Deployment, one replica, three ordinary containers, same combined image and explicit `--role` args.
- Image entrypoint already contains tini. No Kubernetes hydration init container.
- Channels and Harness receive the same existing operator environment for this increment.
- Full agent configuration is mounted only in Harness.
- Engine receives only the operator-resolved variables explicitly selected by `engine.forwardEnv`, never an unrestricted `envFrom`. The mini-harness retains eligible values from its own environment when starting native children. In standalone, the parent performs this selection when spawning its mini-harness child.
- Keep the two existing IPC directory volumes scoped to C/H and H/E.
- Add the generic temporary transfer mount shared read/write by H/E.
- Keep the private HOME/state and shared workspace mounts described above.
- Preserve public ingress on Channels, Recreate, one replica, restricted contexts and no service-account token.

Kubernetes probes use HTTP directly: Channels on 8080, Harness on 8090, Engine on 8081, bound to `0.0.0.0`. Startup and readiness query `/readyz`; liveness queries `/healthz`. Harness and Engine TCP listeners expose health endpoints only. No exec probes or socket clients are required. Startup defaults: initial delay 15 seconds, period 5 seconds, timeout 3 seconds, six failures. The startup probe gates liveness; internal discovery remains available before startup is successful. Standalone retains its configured public port and uses the same initialization and downstream-readiness rules.

The operator does not implement download, copy, native installation, batch removal or tool-specific storage migration.

## 10. Compatibility and scope

The prior permanent `public-context` mount and links into it are superseded by the temporary handoff and engine-owned copies. This changes internal placement, not what skills/prompts/artifacts the engine receives.

Agent implementation must preserve native files and session continuity during supported upgrades. Internal path compatibility is agent-owned; do not solve it with operator branches for each engine/tool. Unsupported/conflicting layouts must fail explicitly rather than discard data.

No new public runtime configuration field, queue, artifact framework, remote TUI, proxy masking, S3 persistence or autoscaler is included. The existing execution and result contracts remain in place.

## 11. Acceptance evidence required before release

- With no channel event submitted, real Pi and OpenCode initialization installs content, generates native configuration, removes the batch and reaches readiness without running a user turn.
- While init is waiting or copying, execution is rejected and readiness/startup health do not report success.
- Missing/corrupt required hydration input, invalid native setup or failed batch deletion prevents readiness and makes the mini-harness exit nonzero.
- After transfer deletion, installed skills, prompts and artifacts remain usable; no installed link resolves into the deleted batch.
- H resolves configured prompt text before handoff and can still execute prepare with E HOME inaccessible.
- Two events reuse the existing native session and workspace. Channel cleanup remains separate from batch deletion.
- A new engine-container instance receives a fresh batch before becoming ready; persistent session data survives.
- Persistent and nonpersistent mounts both work. E cannot read H private state/config; H cannot read E HOME.
- Existing full test/lint/schema gates and real three-container acceptance pass.
