# Handoff, Session Hooks and Agent Sandbox Sessions — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** (A) Replace `channel.prepare`/`channel.cleanup` with `channel.handoff` + agent-level
`hooks.sessionStart`/`hooks.sessionSuspend`, identical in every placement. (B) Add the
`sandboxed` placement: one `kubernetes-sigs/agent-sandbox` sandbox per session, bot HOME
persisted as tar.gz (hot → harness PVC 1 day → S3 60 days), no ACH credential ever inside.

**Architecture:** Placements differ only in *where* the engine runs, never in options:
`standalone` runs the mini-harness (`--role engine`) as a local child over a Unix socket;
`sandboxed` runs it in a claimed sandbox over TCP. Both speak the same execution API, so handoff,
hooks and new-session detection are implemented once in the mini-harness and the runner. For
`sandboxed`, the harness keeps no registry (Kubernetes is the registry; per-sandbox secrets are
`HMAC(K, purpose:claim)`), reaches the sandbox over TCP with a derived bearer, and is reached back
only through the `FacadeGateway`, which also receives the HOME archive the mini-harness pushes at
suspend time.

**Tech Stack:** Python 3.12, aiohttp, httpx, FastAPI/uvicorn, stdlib `tarfile` (filter="data"),
stdlib `hmac`, `boto3` (S3 via EKS Pod Identity), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-sandbox-sessions-design.md`, including
"Amendments 2026-09-28" (they supersede the body).

**Order:** Part A first (it ships on its own, in `standalone`). Part B builds on it. Removing the
`distributed` placement is a separate follow-up plan (it touches the same files; do it after).

> **Status:** design settled 2026-09-28. One unverified input: the agent-sandbox CRD field paths
> (Task 8 "Read first" — `kubectl explain` before coding).

---

## Decisions (settled 2026-09-27/28)

| # | Decision |
|---|---|
| S1 | Sandbox lifecycle: `kubernetes-sigs/agent-sandbox` (`SandboxWarmPool` + `SandboxClaim`). kagent / agent-substrate: reference only. |
| S2 | The harness creates/deletes claims. Task (sessionless) → claim per invocation. Bot → one claim per `session_key`. |
| S3 | In-sandbox process = existing mini-harness (`--role engine`), not sandboxd. Engine driver runs inside the sandbox (accepted deviation from the 2026-09-10 isolation spec). |
| S4 | Harness ⇄ sandbox trust: NetworkPolicy + per-sandbox tokens. No mTLS yet. |
| S5 | Bot session state = whole HOME (repo included) as tar.gz. Key `<ns>/<agent>/<digest>.tar.gz`, `digest = sha256(session_key)[:32]`. EFS/NFS is not an option. |
| S6 | Resume tiers: hot sandbox (idle window) → harness PVC cache (1 day) → S3 (60 days, bucket lifecycle). |
| S7 | Sandbox stop is sandbox-initiated: the mini-harness owns the idle timer, stops the engine (running `sessionSuspend`), packs HOME and pushes it to the gateway; any harness holding K accepts it. |
| S8 | Kubernetes is the registry. No SQLite/Postgres. Claim name + `ach.ackstorm.ai/session` label carry the digest; tokens derived from K; `lifecycle.shutdownTime` is the idle backstop. |
| S9 | Every harness-owned agent config is rewritten: opencode.json + system-prompt file, pi models/settings/mcp.json (every launch), hydrated skills / `.ach-state/prompts` / `.ach-state/artifacts` (every configure). Agent-planted extra files stay — they only run in the agent's own credential-free sandbox. |
| S10 | `mcpServers` passthrough credentials still reach the engine (documented exception) and may be captured in the HOME archive. |
| S11 | S3 via EKS Pod Identity on the harness SA (Terraform). The sandbox never holds S3 access. |
| S12 | Phase 1 = one harness replica. |
| S13 | Harness restart mid-turn: the in-flight turn fails; the session survives (mini-harness releases the lost controller and accepts the new one). |
| S14 | `channel.prepare` → **`channel.handoff`**: credentialed harness script, run in an empty harness staging dir (`$ACH_HANDOFF_DIR`); its output **replaces** `<session workspace>/handoff`. Content-agnostic. Cadence `handoff.scope`: **`event`** (default — every invocation, like today's prepare) or **`session`** (only for a new session). |
| S15 | `channel.cleanup` is **removed**. Agent-level `hooks.sessionStart` (once per session, after the handoff, before the first turn; fail-closed) and `hooks.sessionSuspend` (every time the session's engine stops, before any HOME archive; may run many times; best-effort) run in the mini-harness with only `engine.forwardEnv` variables. |
| S16 | The job/steps workflow model is parked as a future idea. |
| S17 | Placements: **`standalone`** (local, quick k8s tests) and **`sandboxed`**. **`distributed` is removed** (separate follow-up plan). Same options everywhere. |
| S18 | No babysitting: no workspace retention sweep. Disk growth in `standalone` without `cleanup` is the operator's concern. |

## Findings from the code (verified 2026-09-28)

1. **`/t/<token>/` is correlation, not authentication** (`engine/base/pool.py:405` mints it;
   `engine/mcp_proxy.py:303-305` forwards unknown tokens; FastMCP facades untokenized). → the
   `FacadeGateway` is the sandbox → harness boundary; facades stay on `127.0.0.1` unchanged.
2. **The execution API is served only on a Unix socket** (`boot/roles.py:run_engine`) → sandbox
   mode adds a TCP listener behind a bearer.
3. **`configure` installs hydration and opens `NativeSessionStore(home)`**
   (`execution/service.py:236-330`) → a restored HOME and the hydration batch are imported before
   controller-open.
4. **Config rewrite (S9) holds by construction** (`engine/lifecycle.py:397,536,627`,
   `engine/pi/driver.py:67-71`, `engine/context.py:_copy_tree_contents`) → regression tests only.
5. **The hydration batch is deleted by the engine after install** → the harness packs it once and
   imports a copy into every new sandbox.
6. **The session workspace is created by the mini-harness** (`execution/service.py:648`
   `prepare_workspace` → `engine/workspace.py:30`), only when the channel has prepare/cleanup
   today → "new session" = that directory did not exist before this reservation. Same rule in
   every placement (a restored sandbox HOME already contains it).
7. **`cleanup` is a large machinery** — `boot/cleanup_registry.py` (136 lines), workspace barriers
   and `WorkspaceStoppedEvent`/cleanup-ack routes in `execution/service.py` / `app.py` / `wire.py`,
   the pump in `engine_runner`, `run_cleanup` in `boot/prepare.py`. S15 deletes all of it.
8. **Hooks reuse `boot/prepare.py:_execute_hook`** (bounded, process-group kill, output tail).

## Session lifecycle (all placements)

```
event ─▶ runner: WorkspacePrepareRequest ─▶ mini-harness creates/finds <workDir>/<slug-hash>
          new_session = directory was just created
handoff runs if scope=event, or scope=session and the session is new:
  harness: handoff script in empty staging ($ACH_HANDOFF_DIR) ── tar.gz ──▶ replaces <workspace>/handoff
new session: mini-harness: hooks.sessionStart (cwd workspace; fail-closed)   ← after the handoff
every invocation: acquire (cwd workspace) ─▶ turn
engine for the session stops (pool idle TTL, stop_all, sandbox idle):
  mini-harness: hooks.sessionSuspend (best-effort) ─▶ native stop
sandboxed only, sandbox idle: after the stop above ─▶ tar.gz of HOME ─▶ gateway ─▶ PVC/S3 ─▶ delete claim
```

- The per-session workspace (and therefore handoff/new-session detection) is used when the
  channel has `handoff` or the agent has any hook — today's behaviour for prepare/cleanup.
  Otherwise the engine cwd stays `workDir` exactly as today.
- Sandbox layout: `HOME=/home/agent`, `workDir=/home/agent/workspace` (inside HOME, so repos are
  in the archive).
- `<workspace>/handoff` is **harness-owned**: every handoff run replaces it wholesale (extract to a
  sibling temp dir, then swap). With `scope: event`, anything the agent changed inside `handoff/`
  is gone on the next event — the agent works on a copy or elsewhere in the workspace. A handoff
  cannot `git fetch` into a previous clone: it always starts empty (the harness never sees the
  sandbox's files), so an event-scoped repo handoff is a fresh (ideally shallow) clone each time.
- Migration: `gitlab-pr` (`aws-nglz-genai/.../achagents/gitlab-pr.yaml`) drops `cleanup` (owner
  does it) and renames `prepare` → `handoff` (`scope: event` keeps today's per-event run; its
  script must clone into `$ACH_HANDOFF_DIR` from scratch instead of fetching into an existing repo).

---

## Layout

```
src/ach_agent/sandbox/
  __init__.py
  archive.py     # pack / capped write / safe extract (stdlib tarfile)
  tokens.py      # digest, claim_name, HMAC-derived bearer + facade token
  store.py       # SessionStore: PVC cache ⇄ S3 keyed by digest, 1-day sweep, upload retry
  claims.py      # ClaimClient: SandboxClaim create / get / list / touch / delete
  gateway.py     # FacadeGateway: token-checked relay + session-archive push
  sessions.py    # SandboxSessions: stateless lease(event) → connected ExecutionClient
```

Inner loop: one test file at a time. `make verify` once, at the end (Task 15).

---

# Part A — handoff and session hooks (all placements)

### Task 1: Config — `handoff`, `hooks`, remove `prepare`/`cleanup`

**Files:**
- Modify: `src/ach_agent/config/schema.py` (`ChannelConfig:751`, `PrepareBlock:707`, `AgentConfig`)
- Modify: `src/ach_agent/boot/secrets.py` (secret-name collection: `prepare` → `handoff`)
- Modify: every test/fixture using `prepare:`/`cleanup:` (`grep -rn "prepare\|cleanup" tests example.yaml docs/schemas`)
- Test: `tests/config/test_handoff_hooks.py`
- Regenerate: `make schema`

**Step 1: Failing tests**

```python
# tests/config/test_handoff_hooks.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.boot.secrets import collect_secret_env_names
from ach_agent.config.schema import AgentConfig

# Copy a minimal valid config (with one webhook channel) from tests/config fixtures.
from tests.config.fixtures import minimal_config  # adjust to the real helper


def test_prepare_and_cleanup_are_gone() -> None:
    for field in ("prepare", "cleanup"):
        raw = minimal_config()
        raw["channels"][0][field] = {"script": "true"}
        with pytest.raises(ValidationError):
            AgentConfig.model_validate(raw)


def test_handoff_and_hooks_parse() -> None:
    raw = minimal_config()
    raw["channels"][0]["handoff"] = {"script": "true", "secretEnv": {"TOK": {"env": "TOK"}}}
    assert AgentConfig.model_validate(raw).channels[0].handoff.scope == "event"  # default
    raw["channels"][0]["handoff"]["scope"] = "session"
    raw["hooks"] = {"sessionStart": {"script": "true"}, "sessionSuspend": {"script": "true"}}
    cfg = AgentConfig.model_validate(raw)
    assert cfg.channels[0].handoff is not None
    assert cfg.hooks.session_suspend is not None
    assert "TOK" in collect_secret_env_names(cfg)


def test_hooks_reject_secret_env() -> None:
    raw = minimal_config()
    raw["hooks"] = {"sessionStart": {"script": "true", "secretEnv": {"X": {"env": "X"}}}}
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(raw)


def test_timeouts_fit_the_lane() -> None:
    raw = minimal_config()
    raw["limits"] = {"maxInvocationSeconds": 60}
    raw["hooks"] = {"sessionStart": {"script": "true", "timeoutSeconds": 120}}
    with pytest.raises(ValidationError, match="sessionStart"):
        AgentConfig.model_validate(raw)
```

(Match the `SecretSource` shape used by existing `secretEnv` tests.)

**Step 2:** run → FAIL.

**Step 3: Implement**

- `ChannelConfig`: delete `prepare` and `cleanup`; add `handoff: HandoffBlock | None = None`,
  where `HandoffBlock(PrepareBlock)` adds `scope: Literal["event", "session"] = "event"`.
  `PrepareBlock` stays for `webhook-script`; docstring: handoff runs in the harness, in an empty
  `$ACH_HANDOFF_DIR`, every event or once per new session.
- New `HookBlock` (`script`, `timeoutSeconds` 1..3600, default 120; `extra="forbid"` — no
  `secretEnv`, by construction) and `HooksBlock` (`sessionStart`, `sessionSuspend`, both optional);
  `AgentConfig.hooks: HooksBlock = Field(default_factory=HooksBlock)`.
- `_hook_timeouts_fit_the_lane`: fields `("handoff", "script")` per channel, plus
  `hooks.sessionStart` (it runs inside the invocation). `sessionSuspend` runs outside any lane —
  no check.
- `boot/secrets.py`: collect `handoff.secretEnv` names where `prepare.secretEnv` were collected.
- Fix every compile/test break from the removed fields (Task 3 deletes the runtime code; here
  only make the config layer consistent — leave `run_cleanup` & co. for Task 3).

**Step 4:** `uv run pytest tests/config -q` → PASS; `make schema`.

**Step 5: Commit** — `feat(config)!: replace prepare/cleanup with handoff and session hooks`
(body: BREAKING CHANGE — operator renders `handoff`/`hooks`; see `../ach` follow-up).

---

### Task 2: Mini-harness — new-session detection, handoff import, sessionStart, sessionSuspend

**Read first:** `execution/service.py:176-770` (`prepare_workspace`, reservations, `pool`),
`engine/base/pool.py:491-595` (`_expire`, `_stop_locked`, `stop_all`), `boot/prepare.py:_execute_hook`,
`execution/app.py` route style.

**Files:**
- Modify: `src/ach_agent/execution/wire.py` — `PublicEngineConfig`: `hook_session_start: HookSpec | None = None`,
  `hook_session_suspend: HookSpec | None = None` (`HookSpec = {script, timeout_seconds}`);
  `WorkspacePrepareRequest` response gains `new_session: bool`.
- Modify: `src/ach_agent/engine/base/pool.py` — `EnginePool(on_stop: Callable[[str], Awaitable[None]] | None = None)`;
  awaited (exceptions logged, never raised) in `_stop_locked` **before** the native stop.
- Modify: `src/ach_agent/execution/service.py`, `src/ach_agent/execution/app.py`
- Test: `tests/execution/test_session_hooks.py`, `tests/engine/test_pool_on_stop.py`

**Contract (UDS app and, later, the sandbox TCP app alike):**

| Route | When | Effect |
|---|---|---|
| `POST /execution/v1/workspace/prepare` (existing) | — | response adds `"new_session": true` iff the workspace directory did not exist before this call |
| `PUT /execution/v1/workspace/handoff?invocation_id=…` | a live reservation for that invocation, before acquire | tar.gz (capped) extracted with the `data` filter into a sibling temp dir, then swapped in as `<workspace>/handoff` (the previous one is removed) |
| `POST /execution/v1/workspace/session-start` `{controller_id, invocation_id}` | same | runs `hook_session_start` once (cwd = workspace; env = forwardEnv allowlist from `public.engine_env_names` + `ACH_WORKSPACE`, `ACH_HANDOFF_DIR`, `ACH_SESSION_KEY`); non-zero/timeout → 500 `"sessionStart failed"`; no hook → 200 |
| pool `on_stop(session_key)` | engine for that session stops (idle TTL, discard, `stop_all`) | runs `hook_session_suspend` in that session's workspace if it exists; best-effort, logged |

**Step 1: Failing tests** — new_session true then false for the same key; handoff lands under
`<workspace>/handoff` and a traversal archive is rejected; session-start runs the hook once, sees a
forwardEnv var and not an unrelated one, 500 on `exit 3`; `on_stop` fires before the native stop
for `_expire`, `discard` and `stop_all`, and a raising callback does not break the stop.

**Step 3: Implement** — archive work through `ach_agent.sandbox.archive` (Task 5 lands first if
you prefer strict order; otherwise add `archive.py` here and Task 5 only adds tests).

**Step 4 / 5:** PASS; commit `feat(execution): session hooks and handoff import in the mini-harness`.

---

### Task 3: Delete the cleanup machinery

**Files:** `src/ach_agent/boot/cleanup_registry.py` (delete), `tests/test_cleanup_registry.py`
(delete), `src/ach_agent/boot/prepare.py` (`run_cleanup`, cleanup metrics), `src/ach_agent/engine/metrics.py`
(`CLEANUP_FAILURES`), `src/ach_agent/execution/{wire,service,app}.py` (`WorkspaceStoppedEvent`,
`WorkspaceCleanupAckRequest`, `cleanup-ack` route, workspace barriers, `notify_on_stop` /
`cleanup_ack_required` / `cleanup_timeout_seconds` on `WorkspacePrepareRequest`),
`src/ach_agent/boot/execution_client.py` (`next_controller_event`, `ack_workspace_cleanup`),
`src/ach_agent/boot/engine_runner.py` (`cleanup_events` pump, `registry`, `private_registered`).

**Rule:** the held controller stream stays (it is the liveness channel); only its
`workspace_stopped` event type goes. Delete, run `uv run pytest -q -x`, repeat.

**Done when:** `grep -rn "cleanup_ack\|WorkspaceStopped\|CleanupRegistry\|run_cleanup\|CLEANUP_FAILURES\|notify_on_stop" src tests`
is empty and `uv run pytest -q` passes. Mention in CHANGELOG that `ach_cleanup_failures_total`
is gone.

**Commit** — `refactor!: remove channel cleanup machinery`.

---

### Task 4: Runner — handoff + sessionStart on new sessions

**Read first:** `boot/engine_runner.py:65-470`; `tests/boot/test_engine_runner_http.py`.

**Files:**
- Modify: `src/ach_agent/boot/engine_runner.py`, `src/ach_agent/boot/prepare.py` (`build_prepare_env` adds `ACH_HANDOFF_DIR`)
- Modify: `src/ach_agent/boot/execution_client.py` (`import_handoff(path, invocation_id)`, `start_session(controller_id, invocation_id)`)
- Test: `tests/boot/test_engine_runner_handoff.py`

**Change:** replace the prepare branch. When `ch_cfg.handoff` or any hook is configured:
1. send `WorkspacePrepareRequest` (as today) → `result["new_session"]`;
2. if `handoff` and (`scope == "event"` or new): `staged = Path(tempfile.mkdtemp(dir=<harness state>/staging))`,
   `await run_prepare(handoff_cfg, event, staged)` (existing runner; `ACH_HANDOFF_DIR=staged`,
   `ACH_WORKSPACE=staged`), `archive.pack(staged, tmp)`, `client.import_handoff(tmp, invocation_id)`;
   `shutil.rmtree(staged)` + unlink `tmp` in `finally`;
3. if new: `client.start_session(...)` (runs `sessionStart`; a no-op without the hook);
4. acquire with cwd = workspace (as today).
Failures in 2–3 take today's `PrepareFailed` fail-closed path. Metric names
(`prepare_failures_total`) stay — operators alert on them; rename is not worth the churn.

**Tests:** new + handoff → `run_prepare` gets an empty staging dir, `import_handoff` then
`start_session` then `acquire` (order asserted), staging removed; not new + `scope: event` →
handoff runs, `start_session` does not; not new + `scope: session` → neither; hooks
without handoff → `start_session` only; handoff failure → no acquire, fail-closed; no handoff and
no hooks → no `WorkspacePrepareRequest` (cwd = `workDir`, unchanged).

**Commit** — `feat(runner): run channel handoff and sessionStart on new sessions`.

---

# Part B — sandboxed placement

### Task 5: Archive helpers

**Files:**
- Create: `src/ach_agent/sandbox/__init__.py` (SPDX header + one-line docstring)
- Create: `src/ach_agent/sandbox/archive.py`
- Test: `tests/sandbox/__init__.py`, `tests/sandbox/test_archive.py`

**Step 1: Failing tests**

```python
# tests/sandbox/test_archive.py
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.sandbox.archive import ArchiveTooLarge, extract, pack, write_capped


def test_pack_extract_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "home"
    (src / "workspace" / "repo").mkdir(parents=True)
    (src / "workspace" / "repo" / "f.txt").write_text("hi")
    (src / "link").symlink_to("workspace/repo/f.txt")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000_000)
    assert (dest / "workspace" / "repo" / "f.txt").read_text() == "hi"
    assert (dest / "link").is_symlink()


def test_pack_over_cap_raises_and_removes(tmp_path: Path) -> None:
    src = tmp_path / "home"
    src.mkdir()
    (src / "big").write_bytes(b"\0" * 10_000)
    archive = tmp_path / "a.tar.gz"
    with pytest.raises(ArchiveTooLarge):
        pack(src, archive, max_bytes=10)
    assert not archive.exists()


def _evil(tmp_path: Path, name: str) -> Path:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    return archive


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd"])
def test_extract_rejects_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(tarfile.TarError):
        extract(_evil(tmp_path, name), tmp_path / "out", max_expanded_bytes=100)


def test_extract_rejects_bomb(tmp_path: Path) -> None:
    src = tmp_path / "home"
    src.mkdir()
    (src / "zeros").write_bytes(b"\0" * 1_000_000)
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    with pytest.raises(ArchiveTooLarge):
        extract(archive, tmp_path / "out", max_expanded_bytes=1000)


async def test_write_capped(tmp_path: Path) -> None:
    async def chunks():
        for _ in range(3):
            yield b"abcd"

    dest = tmp_path / "x"
    assert await write_capped(chunks(), dest, max_bytes=12) == 12
    with pytest.raises(ArchiveTooLarge):
        await write_capped(chunks(), tmp_path / "y", max_bytes=11)
    assert not (tmp_path / "y").exists()
```

**Step 2:** `uv run pytest tests/sandbox/test_archive.py -v` → FAIL (module missing).

**Step 3: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""Session archives: tar.gz pack, capped receive, safe extract.

Extraction uses tarfile's ``data`` filter (PEP 706): absolute paths, ``..``, links that
escape the destination, devices and FIFOs are refused. It runs only inside the sandbox —
the harness never extracts an agent-written archive, it only moves the bytes (spec §5).
"""

from __future__ import annotations

import tarfile
from collections.abc import AsyncIterator
from pathlib import Path


class ArchiveTooLarge(ValueError):
    """An archive (or its expansion) exceeds the configured cap."""


def pack(src: Path, dest: Path, *, max_bytes: int) -> int:
    """Write ``src``'s contents to ``dest`` as tar.gz; return the archive size."""
    # ponytail: size is checked after writing; a streaming cap only matters if HOMEs
    # routinely approach maxArchiveBytes.
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src, arcname=".")
    size = dest.stat().st_size
    if size > max_bytes:
        dest.unlink()
        raise ArchiveTooLarge(f"archive is {size} bytes, cap is {max_bytes}")
    return size


async def write_capped(chunks: AsyncIterator[bytes], dest: Path, *, max_bytes: int) -> int:
    """Stream ``chunks`` into ``dest``; remove it and raise past ``max_bytes``."""
    total = 0
    try:
        with dest.open("wb") as fh:
            async for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise ArchiveTooLarge(f"stream exceeds {max_bytes} bytes")
                fh.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return total


def extract(archive: Path, dest: Path, *, max_expanded_bytes: int) -> None:
    """Extract ``archive`` into ``dest`` with the ``data`` filter and an expansion cap."""
    with tarfile.open(archive, "r:gz") as tar:
        if sum(m.size for m in tar.getmembers()) > max_expanded_bytes:
            raise ArchiveTooLarge("archive expands past the cap")
        dest.mkdir(parents=True, exist_ok=True)
        tar.extractall(dest, filter="data")
```

**Step 4:** `uv run pytest tests/sandbox/test_archive.py -v` → PASS.

**Step 5: Commit** — `feat(sandbox): add session archive helpers`.

---

### Task 6: Derived identifiers and tokens

**Files:**
- Create: `src/ach_agent/sandbox/tokens.py`
- Test: `tests/sandbox/test_tokens.py`

**Step 1: Failing tests**

```python
# tests/sandbox/test_tokens.py
from __future__ import annotations

import re

from ach_agent.sandbox.tokens import (
    claim_name,
    digest,
    engine_bearer,
    facade_token,
    verify_facade_token,
)

K = b"k" * 32


def test_digest_is_stable_128_bit_hex() -> None:
    assert digest("gitlab:mr:1") == digest("gitlab:mr:1")
    assert re.fullmatch(r"[0-9a-f]{32}", digest("x"))


def test_claim_name_is_dns_label() -> None:
    name = claim_name("My_Very.Long.Agent.Name.That.Keeps.Going.On", digest("k"))
    assert len(name) <= 63
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)
    assert "." not in name  # facade tokens split on the last '.'


def test_tokens_derive_and_verify() -> None:
    claim = claim_name("bot", digest("k"))
    token = facade_token(K, claim)
    assert verify_facade_token(K, token) == claim
    assert verify_facade_token(b"other" * 8, token) is None
    flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert verify_facade_token(K, flipped) is None
    assert verify_facade_token(K, "x") is None
    assert engine_bearer(K, claim) == engine_bearer(K, claim) != token
```

**Step 2:** run → FAIL.

**Step 3: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""Per-sandbox identifiers and secrets — derived, never stored.

Kubernetes is the registry (a live sandbox is a labelled SandboxClaim), so nothing here is
persisted: a restarted harness re-derives every token from the per-agent key K, which only
the harness holds. The claim name carries a 128-bit digest of the session_key, the same
digest that names the session archive, so no payload-derived text reaches a name or key.
"""

from __future__ import annotations

import hashlib
import hmac
import re

_SLUG = re.compile(r"[^a-z0-9]+")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def claim_name(agent: str, key_digest: str) -> str:
    slug = _SLUG.sub("-", agent.lower())[:26].strip("-") or "agent"
    return f"ach-{slug}-{key_digest}"


def _mac(key: bytes, purpose: str, claim: str) -> str:
    return hmac.new(key, f"{purpose}:{claim}".encode(), hashlib.sha256).hexdigest()


def engine_bearer(key: bytes, claim: str) -> str:
    """Harness → mini-harness bearer for one sandbox."""
    return _mac(key, "engine", claim)


def facade_token(key: bytes, claim: str) -> str:
    """Sandbox → harness gateway token; self-describing so any harness can verify it."""
    return f"{claim}.{_mac(key, 'facade', claim)}"


def verify_facade_token(key: bytes, token: str) -> str | None:
    """Return the claim a token was issued for, or None."""
    claim, sep, mac = token.rpartition(".")
    if not sep or not claim:
        return None
    return claim if hmac.compare_digest(mac, _mac(key, "facade", claim)) else None
```

**Step 4 / 5:** PASS; commit `feat(sandbox): derive sandbox identifiers and tokens`.

---

### Task 7: `SessionStore` (PVC cache ⇄ S3, keyed by digest)

**Files:**
- Modify: `pyproject.toml` + `uv.lock` (`uv add "boto3>=1.35,<2"`)
- Create: `src/ach_agent/sandbox/store.py`
- Test: `tests/sandbox/test_store.py`

**Step 1: Failing tests** (fake S3, no network)

```python
# tests/sandbox/test_store.py
from __future__ import annotations

import os
import time
from pathlib import Path

from ach_agent.sandbox.store import SessionStore

D = "0" * 32


class _NotFound(Exception):
    response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_upload = False

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        if self.fail_upload:
            raise RuntimeError("s3 down")
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        if key not in self.objects:
            raise _NotFound()
        Path(filename).write_bytes(self.objects[key])


def _store(tmp_path: Path, s3: FakeS3) -> SessionStore:
    return SessionStore(tmp_path / "cache", bucket="b", prefix="ns/bot", cache_ttl_seconds=86400, s3=s3)


async def test_miss_everywhere(tmp_path: Path) -> None:
    assert await _store(tmp_path, FakeS3()).fetch(D) is None


async def test_commit_then_fetch_from_cache(tmp_path: Path) -> None:
    s3 = FakeS3()
    store = _store(tmp_path, s3)
    src = tmp_path / "x.tar.gz"
    src.write_bytes(b"data")
    await store.commit(D, src)
    await store.wait_uploads()
    assert store.object_key(D) == f"ns/bot/{D}.tar.gz"
    assert s3.objects[store.object_key(D)] == b"data"
    path = await store.fetch(D)
    assert path is not None and path.read_bytes() == b"data"


async def test_fetch_falls_back_to_s3(tmp_path: Path) -> None:
    s3 = FakeS3()
    store = _store(tmp_path, s3)
    s3.objects[store.object_key(D)] = b"remote"
    path = await store.fetch(D)
    assert path is not None and path.read_bytes() == b"remote"


async def test_sweep_retries_pending_then_evicts(tmp_path: Path) -> None:
    s3 = FakeS3()
    s3.fail_upload = True
    store = _store(tmp_path, s3)
    src = tmp_path / "x.tar.gz"
    src.write_bytes(b"data")
    await store.commit(D, src)
    await store.wait_uploads()
    old = time.time() - 2 * 86400
    os.utime(store.cache_path(D), (old, old))
    s3.fail_upload = False
    await store.sweep()
    assert s3.objects[store.object_key(D)] == b"data"
    assert not store.cache_path(D).exists()
```

**Step 2:** run → FAIL.

**Step 3: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""Bot-session HOME archives: harness PVC cache in front of S3.

PVC first, S3 second: a pushed archive is safe on the PVC before the claim is deleted, and a
harness restart mid-upload finds it still marked pending and uploads it on the next sweep.
Keyed by the session digest (sandbox.tokens.digest). Only the harness holds S3 access (Pod
Identity); the sandbox never sees this module.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_PENDING = ".pending"


def _not_found(exc: BaseException) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"404", "NoSuchKey", "NotFound"}


class SessionStore:
    def __init__(
        self,
        cache_dir: Path,
        *,
        bucket: str,
        prefix: str,
        cache_ttl_seconds: float,
        s3: Any | None = None,
    ) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._ttl = cache_ttl_seconds
        self._s3 = s3
        self._uploads: set[asyncio.Task[None]] = set()

    @property
    def cache_dir(self) -> Path:
        return self._dir

    def _client(self) -> Any:
        if self._s3 is None:
            import boto3  # Pod Identity via the default credential chain

            self._s3 = boto3.client("s3")
        return self._s3

    def object_key(self, digest: str) -> str:
        return f"{self._prefix}/{digest}.tar.gz"

    def cache_path(self, digest: str) -> Path:
        return self._dir / f"{digest}.tar.gz"

    async def fetch(self, digest: str) -> Path | None:
        """Cached archive, else S3 download into the cache, else None (new session)."""
        path = self.cache_path(digest)
        if path.exists():
            path.touch()
            return path
        part = path.with_suffix(".part")
        try:
            await asyncio.to_thread(
                self._client().download_file, self._bucket, self.object_key(digest), str(part)
            )
        except Exception as exc:
            part.unlink(missing_ok=True)
            if _not_found(exc):
                return None
            raise
        part.replace(path)
        return path

    async def commit(self, digest: str, archive: Path) -> None:
        """Move ``archive`` into the cache and upload it to S3 in the background."""
        path = self.cache_path(digest)
        archive.replace(path)
        Path(f"{path}{_PENDING}").touch()
        task = asyncio.create_task(self._upload(path, self.object_key(digest)))
        self._uploads.add(task)
        task.add_done_callback(self._uploads.discard)

    async def _upload(self, path: Path, key: str) -> None:
        try:
            await asyncio.to_thread(self._client().upload_file, str(path), self._bucket, key)
        except Exception as exc:  # noqa: BLE001 — retried by sweep(); the PVC copy is safe
            log.warning("session store: upload failed", key=key, error=str(exc))
            return
        Path(f"{path}{_PENDING}").unlink(missing_ok=True)

    async def wait_uploads(self) -> None:
        await asyncio.gather(*self._uploads, return_exceptions=True)

    async def sweep(self) -> None:
        """Retry pending uploads, then evict uploaded archives older than the TTL."""
        for marker in self._dir.glob(f"*.tar.gz{_PENDING}"):
            archive = Path(str(marker)[: -len(_PENDING)])
            if archive.exists():
                await self._upload(archive, f"{self._prefix}/{archive.name}")
            else:
                marker.unlink(missing_ok=True)
        cutoff = time.time() - self._ttl
        for archive in self._dir.glob("*.tar.gz"):
            if not Path(f"{archive}{_PENDING}").exists() and archive.stat().st_mtime < cutoff:
                archive.unlink(missing_ok=True)
```

**Step 4 / 5:** PASS; commit `feat(sandbox): add PVC+S3 session store`.

---

### Task 8: `ClaimClient` (Kubernetes is the registry)

**Read first:** verify the installed CRD shape before coding body/status paths:
`kubectl explain sandboxclaim.spec --api-version=extensions.agents.x-k8s.io/v1beta1 --recursive`
and `kubectl explain sandboxclaim.status --recursive` (agent-sandbox v1.0.x). The fields used
below — `spec.warmPoolRef.name`, `spec.lifecycle.{shutdownPolicy,shutdownTime}`,
`status.conditions[Ready]`, `status.sandbox.podIPs` — come from reading the repo, **not** from a
live cluster. `podIPs` may be strings or `{"ip": …}` objects; adapt `_pod_ip`.

**Files:**
- Create: `src/ach_agent/sandbox/claims.py`
- Test: `tests/sandbox/test_claims.py` (`httpx.MockTransport`, temp token file)

**Step 1: Failing tests** — `create` posts the expected body (labels, warm pool, shutdownPolicy
Delete, `shutdownTime` RFC 3339) with the bearer from the token file; `get` returns `None` on
404; `ready_ip` returns the IP only when `Ready=True`; `wait_ready` raises `TimeoutError` at the
deadline (timeout 0.3 s, fake never ready); `wait_deleted` returns when GET turns 404 and raises
`TimeoutError` otherwise; `touch` sends a merge-patch of `shutdownTime`
(`Content-Type: application/merge-patch+json`); `list_agent` uses
`labelSelector=ach.ackstorm.ai/agent=bot`; `delete` treats 404 as success.

**Step 3: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""Minimal in-cluster client for agent-sandbox SandboxClaims — the harness's registry.

A live sandbox IS a SandboxClaim labelled with the agent: the harness keeps no table of its
own. RBAC (rendered by the ach operator) limits the harness SA to sandboxclaims in its own
namespace. No kubernetes client dependency: a handful of REST calls over httpx.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path
from typing import Any

import httpx

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_GV = "extensions.agents.x-k8s.io/v1beta1"
AGENT_LABEL = "ach.ackstorm.ai/agent"
SESSION_LABEL = "ach.ackstorm.ai/session"
PERSISTENT_LABEL = "ach.ackstorm.ai/persistent"


def _rfc3339(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ready_ip(claim: dict[str, Any]) -> str | None:
    status = claim.get("status", {})
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
    )
    ips = status.get("sandbox", {}).get("podIPs") or []
    if not ready or not ips:
        return None
    first = ips[0]
    return str(first["ip"] if isinstance(first, dict) else first)


class ClaimClient:
    def __init__(
        self,
        namespace: str,
        warm_pool: str,
        *,
        base_url: str | None = None,
        token_path: Path = _SA / "token",
        ca_path: Path = _SA / "ca.crt",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._pool = warm_pool
        self._token_path = token_path
        self._base = f"/apis/{_GV}/namespaces/{namespace}/sandboxclaims"
        verify: Any = str(ca_path) if transport is None and ca_path.exists() else True
        self._http = httpx.AsyncClient(
            base_url=base_url or f"https://{host}:{port}", verify=verify, transport=transport, timeout=30.0
        )

    def _headers(self, **extra: str) -> dict[str, str]:
        # Read per call: projected SA tokens rotate.
        return {"Authorization": f"Bearer {self._token_path.read_text().strip()}", **extra}

    async def create(self, name: str, labels: dict[str, str], shutdown_at: dt.datetime) -> None:
        body = {
            "apiVersion": _GV,
            "kind": "SandboxClaim",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "warmPoolRef": {"name": self._pool},
                "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": _rfc3339(shutdown_at)},
            },
        }
        (await self._http.post(self._base, json=body, headers=self._headers())).raise_for_status()

    async def get(self, name: str) -> dict[str, Any] | None:
        resp = await self._http.get(f"{self._base}/{name}", headers=self._headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return dict(resp.json())

    async def wait_ready(self, name: str, *, timeout: float, interval: float = 0.5) -> str:
        """Bounded wait for Ready; return the sandbox pod IP."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            claim = await self.get(name)
            ip = ready_ip(claim) if claim is not None else None
            if ip is not None:
                return ip
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"sandbox claim {name} not Ready within {timeout}s")
            await asyncio.sleep(interval)

    async def wait_deleted(self, name: str, *, timeout: float, interval: float = 0.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while await self.get(name) is not None:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"sandbox claim {name} still present after {timeout}s")
            await asyncio.sleep(interval)

    async def touch(self, name: str, *, shutdown_at: dt.datetime) -> None:
        patch = {"spec": {"lifecycle": {"shutdownTime": _rfc3339(shutdown_at)}}}
        resp = await self._http.patch(
            f"{self._base}/{name}",
            json=patch,
            headers=self._headers(**{"Content-Type": "application/merge-patch+json"}),
        )
        resp.raise_for_status()

    async def list_agent(self, agent_label: str) -> list[dict[str, Any]]:
        resp = await self._http.get(
            self._base, params={"labelSelector": f"{AGENT_LABEL}={agent_label}"}, headers=self._headers()
        )
        resp.raise_for_status()
        return list(resp.json().get("items", []))

    async def delete(self, name: str) -> None:
        resp = await self._http.delete(f"{self._base}/{name}", headers=self._headers())
        if resp.status_code != 404:
            resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()
```

**Step 4 / 5:** PASS; commit `feat(sandbox): add SandboxClaim client`.

---

### Task 9: `FacadeGateway` — authenticated relay + session-archive push

**Files:**
- Create: `src/ach_agent/sandbox/gateway.py`
- Test: `tests/sandbox/test_gateway.py`

**Routes:**

| Route | Effect |
|---|---|
| `* /s/{token}/{port}/{tail}` and `* /t/{trace}/s/{token}/{port}/{tail}` | verify token → claim; claim live; port registered → relay to `127.0.0.1:{port}[/t/{trace}]/{tail}` |
| `PUT /s/{token}/session/archive` | verify token → claim → `on_archive(claim, request)` (Task 13) |
| anything else | 404 |

**Step 1: Failing tests** — start a tiny aiohttp upstream on `127.0.0.1:0` that echoes the path
and streams two chunks; build `FacadeGateway(key=K, is_live=..., on_archive=...)`, `start("127.0.0.1", 0)`:

- `rewrite(f"http://127.0.0.1:{p}/v1", token, public_base=...)` then `GET …/models` → upstream saw `/v1/models`;
- `tokenize_url(rewritten, "trace")` + `/x` → upstream saw `/t/trace/v1/x`;
- a token signed with another key → 404; a valid token whose `is_live` returns False → 404;
- an unregistered port → 404 (never relays to arbitrary loopback ports — the harness's own
  internal listeners live there);
- `rewrite` refuses a non-loopback URL (`ValueError`);
- `PUT /s/{token}/session/archive` reaches `on_archive` with the verified claim; invalid token → 404
  and `on_archive` not called;
- an upstream sending `Content-Encoding: gzip` arrives decoded without that header.

**Step 3: Implement**

```python
# SPDX-License-Identifier: Apache-2.0
"""One pod-network port in front of the loopback facades, for sandboxed engines.

Facades (model proxy, McpProxy, memory, repo checkout, a2a) stay bound to 127.0.0.1 and
unchanged. A sandbox reaches them only through this relay:

    [/t/<trace>]/s/<facade-token>/<port>/<tail>  →  http://127.0.0.1:<port>[/t/<trace>]/<tail>

The facade token is HMAC-derived per sandbox (sandbox.tokens) and verified on every request,
unlike the ``/t/<trace>`` correlation token, which the engine pool mints and no facade checks.
Only registered facade ports are reachable. The relay injects no credential: the facade
behind it does, exactly as on loopback. The same port receives the session archive the
mini-harness pushes at session end — accepted by any harness holding K, so a restarted
harness still takes it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import structlog
from aiohttp import web

from ach_agent.sandbox.tokens import verify_facade_token

log = structlog.get_logger(__name__)

_DROP_REQUEST = frozenset({"host", "content-length"})
# aiohttp decompresses upstream bodies, so content-encoding must not be forwarded.
_DROP_RESPONSE = frozenset({"content-length", "transfer-encoding", "content-encoding"})
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10)

IsLive = Callable[[str], Awaitable[bool]]
OnArchive = Callable[[str, web.Request], Awaitable[web.StreamResponse]]


class FacadeGateway:
    def __init__(self, *, key: bytes, is_live: IsLive, on_archive: OnArchive) -> None:
        self._key = key
        self._is_live = is_live
        self._on_archive = on_archive
        self._ports: set[int] = set()
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    def allow_port(self, port: int) -> None:
        self._ports.add(port)

    def rewrite(self, url: str, token: str, *, public_base: str) -> str:
        """Map a registered loopback facade URL to its gateway URL for one sandbox."""
        parts = urlsplit(url)
        if parts.hostname != "127.0.0.1" or parts.port is None or parts.port not in self._ports:
            raise ValueError("only registered loopback facade URLs can be rewritten")
        base = urlsplit(public_base)
        return urlunsplit(
            (base.scheme, base.netloc, f"/s/{token}/{parts.port}{parts.path}", parts.query, "")
        )

    async def start(self, host: str, port: int) -> int:
        self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        app = web.Application(client_max_size=0)  # archive size is capped by the handler
        app.router.add_put("/s/{token}/session/archive", self._archive)
        app.router.add_route("*", r"/t/{trace}/s/{token}/{port:\d+}/{tail:.*}", self._relay)
        app.router.add_route("*", r"/s/{token}/{port:\d+}/{tail:.*}", self._relay)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        await self._runner.setup()
        await web.TCPSite(self._runner, host=host, port=port).start()
        bound: int = self._runner.addresses[0][1]
        log.info("facade gateway started", port=bound)
        return bound

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _claim(self, request: web.Request) -> str:
        claim = verify_facade_token(self._key, request.match_info["token"])
        if claim is None or not await self._is_live(claim):
            raise web.HTTPNotFound()
        return claim

    async def _archive(self, request: web.Request) -> web.StreamResponse:
        return await self._on_archive(await self._claim(request), request)

    async def _relay(self, request: web.Request) -> web.StreamResponse:
        await self._claim(request)
        port = int(request.match_info["port"])
        if port not in self._ports:
            raise web.HTTPNotFound()
        trace = request.match_info.get("trace")
        prefix = f"/t/{trace}" if trace else ""
        target = f"http://127.0.0.1:{port}{prefix}/{request.match_info['tail']}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        assert self._session is not None
        body = await request.read()
        async with self._session.request(
            request.method, target, headers=headers, params=request.query, data=body or None
        ) as upstream:
            resp = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in _DROP_RESPONSE:
                    resp.headers[k] = v
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientError) as exc:
                log.debug("facade gateway: client gone mid-stream", error=str(exc))
            return resp
```

**Step 4 / 5:** PASS; commit `feat(sandbox): add authenticated facade gateway`.

---

### Task 10: `sandbox` config block

**Files:**
- Modify: `src/ach_agent/config/schema.py` (new blocks before `AgentConfig`; field + validator on `AgentConfig`)
- Test: `tests/config/test_sandbox_block.py`
- Regenerate: `docs/schemas/agent-config-v1.schema.json` (`make schema`)

**Step 1: Write the failing test**

```python
# tests/config/test_sandbox_block.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import AgentConfig

# Copy a minimal valid config from tests/config fixtures rather than trusting this literal.
_BASE = {"schemaVersion": "1", "agent": {"name": "bot"}, "model": {"name": "m"}, "capability": {}}
_SB = {"enabled": True, "warmPool": "wp", "gatewayHost": "h", "sessions": {"bucket": "b"}}


def _cfg(**extra: object) -> AgentConfig:
    return AgentConfig.model_validate({**_BASE, **extra})


def test_sandbox_defaults_off() -> None:
    assert _cfg().sandbox.enabled is False


def test_requires_persistence() -> None:
    with pytest.raises(ValidationError, match="persistence"):
        _cfg(sandbox=_SB)


def test_requires_bucket_pool_and_host() -> None:
    for missing in ("warmPool", "gatewayHost"):
        with pytest.raises(ValidationError, match=missing):
            _cfg(persistence={"enabled": True}, sandbox={**_SB, missing: ""})
    with pytest.raises(ValidationError, match="bucket"):
        _cfg(persistence={"enabled": True}, sandbox={**_SB, "sessions": {}})


def test_defaults() -> None:
    sb = _cfg(persistence={"enabled": True}, sandbox=_SB).sandbox
    assert sb.idle_seconds == 900
    assert sb.key_env == "ACH_SANDBOX_KEY"
    assert sb.sessions.cache_ttl_seconds == 86400
```

**Step 2:** `uv run pytest tests/config/test_sandbox_block.py -v` → FAIL (no `sandbox` field).

**Step 3: Implement**

```python
class SandboxSessionsBlock(BaseModel):
    """Where bot-session HOME archives live."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bucket: str = ""
    # Object key = <prefix>/<digest>.tar.gz; empty → "<POD_NAMESPACE>/<agent.name>".
    prefix: str = ""
    cache_ttl_seconds: float = Field(default=86400, gt=0, alias="cacheTtlSeconds")
    max_archive_bytes: int = Field(default=2 * 1024**3, gt=0, alias="maxArchiveBytes")


class SandboxBlock(BaseModel):
    """Run the engine in agent-sandbox pods. Off by default."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    warm_pool: str = Field(default="", alias="warmPool")
    # Address the sandbox uses to reach this harness (Service DNS).
    gateway_host: str = Field(default="", alias="gatewayHost")
    gateway_port: int = Field(default=8095, alias="gatewayPort")
    engine_port: int = Field(default=8082, alias="enginePort")
    # HOME inside the sandbox image; public.home is pinned to it.
    home: str = "/home/agent"
    idle_seconds: float = Field(default=900, ge=0, alias="idleSeconds")
    ready_timeout_seconds: float = Field(default=120, gt=0, alias="readyTimeoutSeconds")
    # NAME of the env var holding the per-agent HMAC key K (a Secret the operator renders into
    # the harness only). A name, never a value.
    key_env: str = Field(default="ACH_SANDBOX_KEY", alias="keyEnv")
    sessions: SandboxSessionsBlock = Field(default_factory=SandboxSessionsBlock)
```

On `AgentConfig`: `sandbox: SandboxBlock = Field(default_factory=SandboxBlock)` plus:

```python
    @model_validator(mode="after")
    def _sandbox_requirements(self) -> AgentConfig:
        sb = self.sandbox
        if not sb.enabled:
            return self
        if not self.persistence.enabled:
            raise ValueError("sandbox.enabled requires persistence.enabled (session cache PVC)")
        if not sb.sessions.bucket:
            raise ValueError("sandbox.enabled requires sandbox.sessions.bucket")
        for name, value in (("warmPool", sb.warm_pool), ("gatewayHost", sb.gateway_host)):
            if not value:
                raise ValueError(f"sandbox.enabled requires sandbox.{name}")
        return self
```

Also add `key_env`'s NAME to the secret-name collection so it is stripped from the engine env and
redacted in logs: find where `prepare.secretEnv` names are collected
(`boot/secrets.py:collect_secret_env_names`) and add `cfg.sandbox.key_env` when enabled, with a
test in `tests/test_secret_forward_guard.py`.

**Step 4:** tests PASS; `make schema`.

**Step 5: Commit**

```bash
git add src/ach_agent/config/schema.py src/ach_agent/boot/secrets.py tests/config/test_sandbox_block.py tests/test_secret_forward_guard.py docs/schemas/agent-config-v1.schema.json
git commit -m "feat(config): add sandbox block for agent-sandbox sessions"
```

---


### Task 11: Mini-harness sandbox mode — TCP, bearer, HOME/hydration import, idle push

**Read first:** `execution/app.py`, `execution/service.py` after Task 2/3, `boot/roles.py:run_engine`.

**Files:**
- Modify: `src/ach_agent/execution/wire.py` (`PublicEngineConfig`: `session_archive_url: str = ""`, `idle_seconds: float = 0`)
- Modify: `src/ach_agent/execution/service.py`, `src/ach_agent/execution/app.py`, `src/ach_agent/boot/roles.py`
- Test: `tests/execution/test_sandbox_mode.py`

**Contract (added only by `create_execution_app(service, sandbox=True)`):**

| Route / behaviour | Auth | When | Effect |
|---|---|---|---|
| `POST /execution/v1/sandbox/token` `{"token"}` | none | — | first call stores the bearer; same token again → 200; a different one → 409 |
| `PUT /execution/v1/sandbox/archive/home` | bearer | unconfigured | tar.gz (capped) extracted into `ACH_SANDBOX_HOME` (restore) |
| `PUT /execution/v1/sandbox/archive/hydration` | bearer | unconfigured | extracted into `/run/ach-agent/transfer/sandbox-batch`; returns `{"path"}` |
| `POST /execution/v1/sandbox/close` | bearer | configured, no active invocation | run the stop path now (end of a sessionless sandbox); responds when done |
| **idle watchdog** | — | `idle_seconds > 0` | no invocation / reservation for `idle_seconds` → stop path |
| **stop path** | — | — | close admission → `pool.stop_all()` (fires `sessionSuspend` per session via `on_stop`, Task 2) → if `session_archive_url`: pack HOME to `/tmp`, `PUT` it (3 attempts, backoff 1/2/4 s) → stay closed. Push failure: log; `shutdownTime` reaps the claim (accepted loss, S7) |
| `GET /execution/v1/health` | none | — | adds `"configured"`, `"closing"` |
| controller-open / acquire while closing | bearer | closing | 409 `{"detail": "session closing"}` |
| every other `/execution/…` route | bearer | — | 401 without it |

The watchdog runs without a controller (harness gone): that is what makes S7 survive a harness
restart. Bounded loop (1 s tick), exits when the service closes. Bearer compare with
`hmac.compare_digest`. Temp archives in `/tmp`, never under HOME. Caps:
`ACH_SANDBOX_MAX_ARCHIVE_BYTES`, expansion 8×. `run_engine`: `ACH_ENGINE_LISTEN=tcp` serves the
sandbox app on `0.0.0.0:${ACH_ENGINE_EXEC_PORT:-8082}` instead of the UDS listener.

**Tests:** one per row; stop path runs `sessionSuspend` **before** packing (the hook writes a
marker; the pushed archive contains it); watchdog pushes once after `idle_seconds=0.2`, never
while an invocation is registered; `close` without archive URL pushes nothing; `sandbox=False`
app unchanged.

**Commit** — `feat(execution): sandbox mode for the mini-harness`.

---

### Task 12: `ExecutionClient` — bearer, archive import, sandbox health

**Files:** `src/ach_agent/boot/execution_client.py`; test `tests/boot/test_execution_client_sandbox.py` (`httpx.MockTransport`).

`auth_token=` adds `Authorization: Bearer …` to all internal `httpx.AsyncClient`s;
`set_sandbox_token(token)` posts without it; `import_archive(path, "home"|"hydration") -> str`;
`sandbox_health() -> dict`; `close_session()`; `failed` = `self._failed or self._controller_lost`;
`connect` surfaces 409 "session closing" as `ExecutionClientError(status_code=409)`.

**Commit** — `feat(execution-client): sandbox bearer and archive import`.

---

### Task 13: `SandboxSessions` — stateless lease

**Files:** `src/ach_agent/sandbox/sessions.py`; test `tests/sandbox/test_sessions.py` (dict-backed fake `ClaimClient`, fake `SessionStore`, recording client factory).

```python
@dataclass
class Sandbox:
    claim: str
    client: ExecutionClient
    facade_token: str

class SandboxSessions:
    async def boot(self) -> None                   # list claims → live cache; store.sweep()
    async def is_live(self, claim: str) -> bool    # gateway callback; cache, then GET
    async def on_archive(self, claim: str, request: web.Request) -> web.StreamResponse
    def lease(self, event: MessageEvent, *, persistent: bool) -> AsyncContextManager[Sandbox]
    def engine_config(self, box: Sandbox, cfg: PublicEngineConfig) -> PublicEngineConfig
    async def close(self) -> None                  # close clients only; sandboxes keep running
```

New-session detection, handoff and sessionStart are **not** here: the runner does them through
the client exactly as in `standalone` (Task 4). A restored HOME already contains the workspace, so
the mini-harness reports `new_session=false` by itself.

**Lease** (per-claim `asyncio.Lock`; only caches in memory):
1. `d = digest(session_key)` (persistent) or `digest(uuid4().hex)`; `name = claim_name(agent, d)`.
2. `claim = await claims.get(name)` (persistent only).
3. **Exists:** cached client if not `failed`; else client with `engine_bearer(K, name)`,
   `set_sandbox_token`, `sandbox_health()`: `closing` → `wait_deleted` → step 4; not `configured`
   → delete + `wait_deleted` → step 4; else `connect(engine_config(...))`, no imports.
   A `connect` 409 "session closing" takes the `closing` branch.
4. **Absent:** in parallel `claims.create(...)` + `wait_ready` **and** `store.fetch(d)`; then
   `set_sandbox_token`, import `home` if found, import `hydration`, `connect(engine_config(cfg with
   hydration_dir))`. On failure: close client, delete claim, re-raise.
5. Before yielding: `claims.touch(name, shutdown_at=now + idle + maxInvocation + 300 s)`.
6. Exit: persistent → nothing (idle push). Non-persistent → `close_session()` (runs
   `sessionSuspend`, no push), close client, `claims.delete(name)`.

**`engine_config`:** rewrite `model_base_url`, every `mcp_local_urls` / `mcp_servers` value via
`gateway.rewrite(..., facade_token(K, claim), public_base=f"http://{gateway_host}:{gateway_port}")`;
`idle_seconds` (0 for non-persistent); `session_archive_url` =
`{public_base}/s/{token}/session/archive` (empty for non-persistent); `mcp_templates` untouched (S10).

**`on_archive`:** claim's `SESSION_LABEL` → stream body via `archive.write_capped` into
`store.cache_dir/.in-<uuid>.tar.gz` → `store.commit(digest, tmp)` → delete claim → drop caches →
204. Never extracted. Over cap → 413, claim kept (backstop). Non-persistent claim → 409.

**Tests:** non-persistent random claim, no fetch, `close_session` + delete on exit; persistent miss
keeps the claim and the second lease reuses the client; persistent hit imports `home` before
`connect`; hydration imported before `connect` with its path in the config; create ‖ fetch
concurrent (event-gated fakes under `wait_for(…, 1)`); **harness restart**: fresh instance
reconnects to an existing claim with the derived bearer, no create, no import; `closing` → waits
then reopens from store; unconfigured → deleted and reopened; `on_archive` commits under the digest,
deletes the claim, never calls `archive.extract`; oversize → 413; `engine_config` rewrites URLs and
leaves `mcp_templates`; open failure deletes the claim.

**Commit** — `feat(sandbox): stateless session lease on the claim registry`.

---

### Task 14: Runner lease + `main.py` wiring

**Files:** `src/ach_agent/boot/engine_runner.py`, `src/ach_agent/main.py`; tests
`tests/boot/test_engine_runner_sandbox.py`, `tests/test_main_sandbox_wiring.py`.

**Runner:** `client: ExecutionClient | None` + `sandboxes: SandboxSessions | None` (exactly one);
body moved into `async def _invoke(client)` unchanged; sandbox mode calls it inside
`async with sandboxes.lease(event, persistent=reuse) as box:` with `box.client`, and applies
`sandboxes.engine_config(box, invocation_engine_cfg)` before the workspace request and acquire.
The Task 4 handoff/sessionStart path is shared as-is.

**main (`cfg.sandbox.enabled`):**
1. `key = os.environ.get(cfg.sandbox.key_env, "").encode()`; empty → exit 1 (fail closed).
2. After facades start: `ClaimClient(POD_NAMESPACE, warm_pool)`, `SessionStore(mount/"sessions", …)`,
   `SandboxSessions(...)`, `FacadeGateway(key=key, is_live=sessions.is_live, on_archive=sessions.on_archive)`;
   `allow_port` for the model proxy and every loopback facade port; `gateway.start("0.0.0.0", gateway_port)`.
3. Pack the boot hydration batch once into `state/hydration.tar.gz`.
4. `public_cfg`: `home = sandbox.home`, `work_dir = f"{sandbox.home}/workspace"`, `persistence_enabled = True`.
5. `await sessions.boot()`; hourly `store.sweep()` (cancelled on shutdown).
6. Skip `LocalEngineProcess`, the UDS connect loop and `import_legacy_sessions`;
   `make_engine_runner(client=None, sandboxes=sessions, …)`.
7. Shutdown: channels → `sessions.close()` (sandboxes keep running and push to whichever harness is
   up) → `gateway.stop()` → facades.

**Tests:** leased client used; `engine_config` applied; non-sandbox path unchanged; `ValueError`
for both/neither; no local engine / UDS connect at boot; allowlist = facade ports; missing key →
exit 1; shutdown order.

**Commit** — `feat: wire the sandboxed placement`.

---

### Task 15: Regressions, docs, verify

- **Config-rewrite regressions (S9):** for `opencode.json`, the system-prompt file, pi
  `models.json`/`settings.json`/`mcp.json`, hydrated `skills/`, `.ach-state/prompts`,
  `.ach-state/artifacts`: write a tampered version into a temp HOME, run the real writer
  (`write_opencode_config`, `PiDriver._prepare_agent_dir`, `install_hydration`), assert it is gone.
  File: `tests/engine/test_config_rewrite.py`.
- `CLAUDE.md`: invariant table "Sandbox mode" row (`sandbox/gateway.py` is the sandbox → harness
  boundary; `sandbox/tokens.py` derives every per-sandbox secret; the harness never extracts an
  agent archive); the `mcpServers` exception now also reaches the S3 archive; event-path table:
  step 3 is `handoff` (new sessions only) + `sessionStart`, step 9 is `sessionSuspend` (no
  cleanup); `sandbox/` in "Where things live".
- `docs/references/README.md`: 2026-09-27 spec, Status "Accepted".
- `CHANGELOG.md`: BREAKING prepare/cleanup → handoff/hooks; `ach_cleanup_failures_total` removed;
  sandboxed placement.
- `make verify` once; fix; re-run.

**Commit** — `docs: handoff, session hooks and sandboxed placement`.

---

## Follow-up plans (separate, in order)

1. **ach-agent — remove the `distributed` placement (S17).** Delete `--role channels` and
   `--role harness` (`boot/roles.py:run_channels`/`run_harness`, channel UDS / `ChannelsClient`,
   the `isolated_harness` branches in `main.py`, `docs/references/2026-09-14-three-role-split.md`
   → Status "Superseded"). Keep `--role engine` (the sandbox mini-harness) and the local launcher.
2. **`../ach` — operator:**
   - placements: `standalone` + `sandboxed`; remove `distributed` rendering;
   - CRD: `channels[].handoff` (shape of `prepare`), `spec.hooks.{sessionStart,sessionSuspend}`
     (`script`, `timeoutSeconds`; no `secretEnv`); remove `prepare`/`cleanup`;
   - `sandboxed`: `SandboxTemplate` (same image, `args: [--role, engine]`, `ACH_ENGINE_LISTEN=tcp`,
     `ACH_SANDBOX_HOME`, `ACH_SANDBOX_MAX_ARCHIVE_BYTES`, `engine.forwardEnv` + `mcpServers` env refs
     as secretKeyRef — the only secrets allowed in the sandbox; `runtimeClassName`;
     `automountServiceAccountToken: false`; `networkPolicyManagement: Unmanaged`) + `SandboxWarmPool`;
     NetworkPolicy (sandbox egress → harness `:gatewayPort` + DNS; ingress ← harness on 8082/8081);
     per-agent Secret K → harness only as `ACH_SANDBOX_KEY`; harness Role for `sandboxclaims`
     (create/get/list/patch/delete), `POD_NAMESPACE`, `sandbox` block in `config.json`, gateway
     Service port, persistence PVC;
   - migrate `gitlab-pr`: drop `cleanup`, `prepare` → `handoff`;
   - `make gen-crd-ref-docs` + `examples/`.
3. **`../aws-nglz-genai/terraform-genai-blueprint-module`:** S3 bucket (SSE, public access
   blocked, lifecycle 60 d), Pod Identity harness SA → role scoped to `bucket/<ns>/*`, gVisor node
   group + `RuntimeClass`, agent-sandbox controller install (if the ach chart does not own it).
