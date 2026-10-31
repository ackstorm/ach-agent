# Storage Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One SeaweedFS storage server per Agent namespace sits between the control pods and S3: saves land on its volume and replicate to the bucket in the background; only it holds AWS credentials.

**Architecture:** The chart renders, for each namespace in `storage.namespaces`, a one-replica StatefulSet `ach-agent-storage` (SeaweedFS `weed server -s3`, a `filer.remote.sync` container, and a shell loop around `weed shell` that mounts the bucket and reconciles), its Service, a generated static S3 key Secret and an Istio AuthorizationPolicy. The operator needs no code change: the chart hands it the endpoint `http://ach-agent-storage:8333` and the namespace-local Secret through the env it already reads, and it already wires both into control pods. The runtime switches to path-style addressing when an endpoint is set and exports save and restore histograms.

**Tech Stack:** Python 3.13 runtime (boto3, prometheus_client, pytest), Helm 3 chart (PyYAML render tests, promtool), SeaweedFS 4.51, Go e2e on kind with Istio ambient.

Spec: `docs/superpowers/specs/2026-10-31-storage-server-design.md`.

## Spec deviations (proposed resolutions)

1. **"Installed by the chart, one per Agent namespace."** The chart is installed once, in the operator namespace (`ach-agent-system`); Agents live in other namespaces. Resolution: a value `storage.namespaces` lists the Agent namespaces; the chart renders the storage objects into each. Helm creates objects in other namespaces of the same release.
2. **No operator code change (spec §5 "operator wiring").** `operator/internal/render/storageconfig.go` already passes `EndpointURL` and a namespace-local `CredentialsSecretName` to control pods, `agent_controller.go` already blocks an Agent until that Secret exists and rolls pods when it changes. A short Service name resolves in the control pod's own namespace, so one endpoint `http://ach-agent-storage:8333` and one Secret name `ach-agent-storage-s3` serve every namespace. The chart sets `ACH_AGENT_STORAGE_S3_ENDPOINT_URL` and `ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET` on the operator Deployment when the storage server is active. An Agent in an unlisted namespace waits with a missing-Secret condition instead of failing silently.
3. **Values `storage.s3.{bucket,region,prefix}` dropped.** `runtime.storage.s3.{bucket,region,endpointUrl,credentialsSecret}` keep meaning "the real bucket and how to reach it"; with the storage server active the chart gives those to the storage server and gives the control pods the storage endpoint. The prefix needs nothing on the server: it mounts the whole bucket, keys are identical (spike check 5).
4. **Evictor loop replaced by SeaweedFS's built-in eviction.** SeaweedFS 4.51 has `-filer.remoteCacheEvictThreshold` (default 0.9): every 30 s, above that disk fraction, it evicts remote-cached objects oldest first. Probed on 2026-10-31 (400 MiB tmpfs volume, threshold 0.5, 20 MiB objects): it evicted synced objects until usage was back under the threshold, `df` dropped, evicted objects read back intact through read-through, and with the remote stopped 4 unsynced objects pushed usage over the threshold repeatedly and all 4 stayed intact (sha256 match). It overshoots a little (a second round runs before the space shows as free). Resolution: `storage.evictThreshold` (default 0.8) replaces `storage.watermarks.{high,low}`; no evictor code.
5. **Backlog signal.** `remote.copy.local -dryRun` lists the remote, so it fails exactly while S3 is unreachable (probed), and `SeaweedFS_filer_last_send_timestamp_of_subscribe` stops advancing whenever nothing is written, so an alert on it would fire on every idle namespace. Resolution: the reconcile loop exports `ach_agent_storage_unsynced_files` (files created more than 5 minutes ago that S3 lacks, from the dry run's `Files to copy: N` line) and `ach_agent_storage_reconcile_ok_timestamp_seconds`; `ACHAgentStorageSyncLagging` uses both. The loop serves them on :9328 with busybox `nc -lk -e` (the image has no httpd).
6. **Reconcile cadence.** Start-up: full `remote.copy.local`. Then every 10 minutes: the dry run with `-minAge=300`, and the upload only when it finds files. Same effect as the spec's unconditional run, plus the metric.
7. **PodMonitor, not ServiceMonitor**, following the chart's existing `metrics.podMonitor` pattern.
8. **`ACHAgentStorageDown` needs kube-state-metrics** (`kube_statefulset_status_replicas_ready`): a pod that does not exist has no scrape target, so `up` cannot see it.
9. **Artifacts go through the storage server too.** The runtime has one S3 client (`storage_from_env` builds it for workspaces, sessions and artifacts). Keeping artifacts on direct S3 would need AWS credentials on control pods, against §1. Resolution: accept; artifacts are only read back through the runtime.
10. **The three `*.allowUntrustedRemoteEndpoints` flags are always set.** The remote endpoint is set only by the chart, and `s3.<region>.amazonaws.com` resolves to private IPs in a VPC with an S3 interface endpoint and private DNS, which the SSRF guard would reject.
11. **e2e has no persistence scenario and no stand-in S3 today** (`test/e2e/fixtures/profile.yaml` disables persistence; `cluster.sh` deploys no S3). Task 4 adds both.
12. **S3 outage latency is worse than the spike's 30 s.** Probed with the remote's DNS name failing: one create-only `PutObject` of a new key took 92 s. botocore's 60 s read timeout fires first, the SDK retries, and the retry gets 412 because the first attempt landed. The runtime already resolves that (`_upload_snapshot_immutable` checks size, sha256 and owner metadata on 412; the registry CAS reconciles through `_reconciled_receipt_if_applied`). No change; the install doc states the numbers.

13. **Bind addresses.** The spike's "config we would ship" (`-ip=127.0.0.1`) binds every port to loopback, so the Service could not reach S3 on 8333. Binding only the internal ports to loopback (`-ip.bind=127.0.0.1 -s3.ip.bind=0.0.0.0`) breaks read-through: the volume server dials S3 from its bind address (`dial tcp 127.0.0.1:0->…: invalid argument`). Resolution: `-ip=127.0.0.1 -ip.bind=0.0.0.0`; the AuthorizationPolicy admits only 8333, 9327 and 9328.

The scripts, `storage.yaml`, the render tests, the promtool tests and `storage_smoke.sh` in this plan were assembled into a scratch copy of the chart and pass (helm 3.16, promtool 3.5.0, SeaweedFS 4.51 in docker) as of 2026-10-31. The runtime and e2e steps were not run.

## Open questions (not blocking; recorded in the report)

- `remote.copy.local` uploads files missing from S3. An overwrite of an existing key (`registry.json` CAS) whose change event a `kill -9` lost is not re-uploaded: S3 keeps the previous registry until the next commit of that ref. Only matters if the volume is then lost (restore falls back one snapshot). Not probed.
- Pod Identity on real EKS: the spike emulated it with `AWS_CONTAINER_AUTHORIZATION_TOKEN`; EKS injects `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`. aws-sdk-go 1.55.8 reads the file variant by source, untested.
- `remote.copy.local` running every 10 minutes alongside `filer.remote.sync` was not probed (only after a restart).

## Out of scope

Release 0.3.0 (`make release-bump VERSION=0.3.0`, `chore(release): v0.3.0`), Terraform (user-owned) and the GitOps bump. Task 2 documents the Pod Identity association in `docs/install.md`; the rollout order is at the end of this plan.

## Global Constraints

- Work only in `/home/coder/workspace/local/ach-agent/.worktrees/specs` (branch `docs/storage-durable-specs`). Never touch `/home/coder/workspace/local/ach-agent`. Do not push.
- Use `/usr/bin/git` for every git command.
- In zsh, brace a variable before a colon: `"${var}:"`, never `"$var:"`.
- English only: code, comments, docs, commit messages.
- Comments state what the code does now and why. No history ("now", "no longer", "replaced"), no task/plan/finding IDs in code, comments or docs.
- SeaweedFS image, everywhere, exactly: `chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8`.
- Python: the uv venv at `runtime/.venv` only; never a system-wide pip install.
- Wrap every long command in `timeout`. Never pipe a gate into `tail`/`grep` and trust the exit code: redirect to a log, `echo "EXIT=$?"`, then grep the log.
- Kubernetes calls use `--context kind-ach-agent-e2e` only (`test/e2e/cluster.sh` and the e2e helpers already do).
- Gates: the scoped commands in each task; `make e2e` once, at the end of Task 4.
- Commit messages: conventional commits, short imperative subject, ending with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
  ```

## File map

| File | Task | Responsibility |
|---|---|---|
| `runtime/src/ach_runtime/storage/config.py` | 1 | Path-style addressing when an endpoint is set |
| `runtime/src/ach_runtime/observability/metrics.py` | 1 | `STORAGE_PERSIST_SECONDS`, `STORAGE_RESTORE_SECONDS` |
| `runtime/src/ach_runtime/harness/lifecycle.py` | 1 | `_storage_timer` around the two restores and two saves |
| `runtime/tests/storage/test_config.py`, `runtime/tests/harness/test_lifecycle.py` | 1 | Tests |
| `chart/files/storage/{reconcile,sync,metrics}.sh` | 2 | Shell around `weed shell` in the SeaweedFS image |
| `chart/templates/storage.yaml` | 2 | Per-namespace Secret, ConfigMap, ServiceAccount, Service, StatefulSet, AuthorizationPolicy |
| `chart/templates/_helpers.tpl`, `deployment.yaml`, `values.yaml`, `values.schema.json` | 2 | Activation, validation, operator env switch, values |
| `chart/tests/render_test.sh`, `chart/tests/storage_smoke.sh`, `Makefile` | 2 | Render checks; docker smoke of the scripts (`make storage-smoke`) |
| `chart/templates/podmonitor.yaml`, `chart/alerts/ach-agent.rules.yaml`, `chart/tests/alerts.test.yaml` | 3 | Scrape + three alerts + promtool tests |
| `test/e2e/s3-standin.yaml`, `test/e2e/cluster.sh`, `test/e2e/fixtures/persist-agent.yaml`, `test/e2e/e2e_test.go`, `test/e2e/helpers_test.go` | 4 | Stand-in S3, storage server on kind, persistence scenario |
| `docs/install.md`, `runtime/docs/schemas/ach-workspace-contract-v1.md`, `CHANGELOG.md`, `CLAUDE.md` | 1–3 | Docs that change with each task |

---

### Task 1: Runtime: path-style endpoint and save/restore histograms

**Files:**
- Modify: `runtime/src/ach_runtime/storage/config.py`
- Modify: `runtime/src/ach_runtime/observability/metrics.py` (after `KEY_LIFETIME`)
- Modify: `runtime/src/ach_runtime/harness/lifecycle.py` (imports; after `_owned`; `_restore_workspace_archive`, `commit_workspace`, `restore_session`, `_save_session`)
- Test: `runtime/tests/storage/test_config.py`, `runtime/tests/harness/test_lifecycle.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing new.
- Produces: Prometheus histograms `ach_agent_storage_persist_seconds{domain,outcome}` (outcome `ok`|`error`) and `ach_agent_storage_restore_seconds{domain,outcome}` (outcome `ok`|`empty`|`error`), `domain` = `workspace`|`session`, served on the control pod's `/metrics` (port `http`, 8080). Task 4 reads `ach_agent_storage_persist_seconds_count` and `ach_agent_storage_restore_seconds_count` from there. With `ACH_STORAGE_S3_ENDPOINT_URL` set, the boto3 client uses path-style addressing.

- [ ] **Step 1: Create the runtime venv (once)**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs/runtime
timeout 600 uv venv .venv --python 3.13
timeout 600 uv pip install --python .venv/bin/python -e ".[dev]"
```

- [ ] **Step 2: Write the failing tests**

In `runtime/tests/storage/test_config.py`, replace in `test_storage_from_env_compatible_settings`:

```python
    client_mock.assert_called_once_with(
        "s3", region_name="us-east-1", endpoint_url="http://seaweedfs:8333"
    )
```

with:

```python
    client_mock.assert_called_once()
    args, kwargs = client_mock.call_args
    assert args == ("s3",)
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["endpoint_url"] == "http://seaweedfs:8333"
    assert kwargs["config"].s3 == {"addressing_style": "path"}
```

(`test_storage_from_env_standard_s3` keeps `client_mock.assert_called_once_with("s3")`: without an endpoint the AWS default addressing stays.)

In `runtime/tests/harness/test_lifecycle.py`, add after `_close` (module level, before the first test):

```python
def _storage_count(metric: str, domain: str, outcome: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(f"{metric}_count", {"domain": domain, "outcome": outcome}) or 0.0
```

In `test_close_transient_export_and_save_retry_one_id_and_bytes`, replace its last lines:

```python
        assert delays == [1, 2, 4]
        await control.mini_client.aclose()

    asyncio.run(scenario())
```

with:

```python
        assert delays == [1, 2, 4]
        await control.mini_client.aclose()

    before = _storage_count("ach_agent_storage_persist_seconds", domain, "ok")
    asyncio.run(scenario())
    assert _storage_count("ach_agent_storage_persist_seconds", domain, "ok") == before + 1
```

Append a new test at the end of the file:

```python
def test_storage_histograms_record_domain_and_outcome() -> None:
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import RestoreReceipt, WriterLease

    restore = "ach_agent_storage_restore_seconds"
    persist = "ach_agent_storage_persist_seconds"
    expected = [(restore, "empty"), (restore, "ok"), (restore, "error"), (persist, "error")]
    before = {key: _storage_count(key[0], "workspace", key[1]) for key in expected}

    async def scenario():
        results = iter([None, "receipt", RuntimeError("restore down")])

        class Storage:
            async def restore(self, ref, *, retention_days=0):
                result = next(results)
                if isinstance(result, Exception):
                    raise result
                if result is None:
                    return None

                async def opened():
                    yield b"stored"

                return RestoreReceipt(ref, "snapshot-1", 4, 1, "etag", "checksum", 6, 1, opened)

            async def acquire_writer(self, ref, writer_instance_id):
                return WriterLease(ref, writer_instance_id, 5, "etag")

            async def persist(self, ref, snapshot_id, stream, lease, expected_base, *, retention_days=0):
                raise RuntimeError("storage rejected the save")

        async def mini(request):
            if request.method == "GET":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "instance-id"}, content=b"archive")
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "instance-id"}, json={"status": "restored"})

        control = _Control()
        control.config.workspace.persistence.enabled = True
        control.storage = Storage()
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                     "workspace", "instance-id", "http://mini")
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        await lifecycle.restore_workspace(endpoint)
        await lifecycle.restore_workspace(endpoint)
        with pytest.raises(RuntimeError, match="restore down"):
            await lifecycle.restore_workspace(endpoint)
        with pytest.raises(RuntimeError, match="storage rejected the save"):
            await lifecycle.commit_workspace(endpoint)
        await control.mini_client.aclose()
        await lifecycle.close()

    asyncio.run(scenario())
    for key, value in before.items():
        assert _storage_count(key[0], "workspace", key[1]) == value + 1, key
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs/runtime
PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness timeout 600 \
  .venv/bin/pytest --import-mode=importlib tests/storage/test_config.py tests/harness/test_lifecycle.py \
  -q -p no:cacheprovider > /tmp/t1.log 2>&1; echo "EXIT=$?"; grep -E 'passed|failed|Error' /tmp/t1.log
```

Expected: `EXIT=1`; failures in `test_storage_from_env_compatible_settings` (`KeyError: 'config'`), both parametrisations of `test_close_transient_export_and_save_retry_one_id_and_bytes`, and `test_storage_histograms_record_domain_and_outcome`.

- [ ] **Step 4: Implement path-style addressing**

In `runtime/src/ach_runtime/storage/config.py`, add the imports:

```python
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config
```

and change the client kwargs block to:

```python
    client_kwargs: dict[str, Any] = {}
    region = env.get("ACH_STORAGE_S3_REGION")
    if region:
        client_kwargs["region_name"] = region
    endpoint = env.get("ACH_STORAGE_S3_ENDPOINT_URL")
    if endpoint:
        client_kwargs["endpoint_url"] = endpoint
        # In-cluster endpoints such as the storage server have no per-bucket DNS names.
        client_kwargs["config"] = Config(s3={"addressing_style": "path"})
```

- [ ] **Step 5: Add the histograms**

In `runtime/src/ach_runtime/observability/metrics.py`, after `KEY_LIFETIME = Gauge(...)`:

```python
# A save covers the archive export from the unit and every retry inside the close window; a
# restore covers the Storage read and streaming the archive into the unit.
_STORAGE_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, float("inf"))
STORAGE_PERSIST_SECONDS = Histogram(
    "ach_agent_storage_persist_seconds", "Workspace or session save duration",
    ["domain", "outcome"], buckets=_STORAGE_BUCKETS,
)
STORAGE_RESTORE_SECONDS = Histogram(
    "ach_agent_storage_restore_seconds", "Workspace or session restore duration",
    ["domain", "outcome"], buckets=_STORAGE_BUCKETS,
)
```

- [ ] **Step 6: Time the saves and restores in the lifecycle**

In `runtime/src/ach_runtime/harness/lifecycle.py`:

Change `from contextlib import asynccontextmanager` to `from contextlib import asynccontextmanager, contextmanager`.

After the `_owned` function add (the import is local like the module's other observability imports):

```python
@contextmanager
def _storage_timer(operation: str, domain: str):
    """Observe ach_agent_storage_<operation>_seconds{domain, outcome} for the block. The block
    sets timer["outcome"]; a block that raises, or sets none, counts as "error"."""
    from ach_runtime.observability.metrics import STORAGE_PERSIST_SECONDS, STORAGE_RESTORE_SECONDS

    histogram = STORAGE_PERSIST_SECONDS if operation == "persist" else STORAGE_RESTORE_SECONDS
    timer = {"outcome": "error"}
    started = time.monotonic()
    try:
        yield timer
    finally:
        histogram.labels(domain=domain, outcome=timer["outcome"]).observe(time.monotonic() - started)
```

In `_restore_workspace_archive`, replace:

```python
            receipt = await self.control.storage.restore(ref, retention_days=retention_days)
            if receipt is not None:
                await put_archive(self.control.mini_client, endpoint, self.control._headers(endpoint),
                                  "/workspace/archive", receipt.open(),
                                  on_mismatch=self.control.manager.report_lost)
                self._bases[(ref.workspace_ref, None)] = receipt.etag
```

with:

```python
            with _storage_timer("restore", DOMAIN_WORKSPACE) as timer:
                receipt = await self.control.storage.restore(ref, retention_days=retention_days)
                if receipt is not None:
                    await put_archive(self.control.mini_client, endpoint, self.control._headers(endpoint),
                                      "/workspace/archive", receipt.open(),
                                      on_mismatch=self.control.manager.report_lost)
                    self._bases[(ref.workspace_ref, None)] = receipt.etag
                timer["outcome"] = "ok" if receipt is not None else "empty"
```

In `commit_workspace`, replace:

```python
        try:
            return await self._persist_close(
                endpoint, ref, "/workspace/archive",
                retention_days=self.control.config.workspace.persistence.retention_days,
            )
        except Exception as exc:
```

with:

```python
        try:
            with _storage_timer("persist", DOMAIN_WORKSPACE) as timer:
                receipt = await self._persist_close(
                    endpoint, ref, "/workspace/archive",
                    retention_days=self.control.config.workspace.persistence.retention_days,
                )
                timer["outcome"] = "ok"
            return receipt
        except Exception as exc:
```

In `restore_session`, replace:

```python
            receipt = await self.control.storage.restore(ref, retention_days=retention_days)
            if receipt is None:
                return
            await put_archive(
                self.control.mini_client, endpoint, self.control._headers(endpoint),
                f"/sessions/{ticket.session_ref}/archive", receipt.open(),
                params={"deadline_at": ticket.deadline_at.isoformat()},
                on_mismatch=self.control.manager.report_lost,
            )
```

with:

```python
            with _storage_timer("restore", DOMAIN_SESSION) as timer:
                receipt = await self.control.storage.restore(ref, retention_days=retention_days)
                if receipt is not None:
                    await put_archive(
                        self.control.mini_client, endpoint, self.control._headers(endpoint),
                        f"/sessions/{ticket.session_ref}/archive", receipt.open(),
                        params={"deadline_at": ticket.deadline_at.isoformat()},
                        on_mismatch=self.control.manager.report_lost,
                    )
                timer["outcome"] = "ok" if receipt is not None else "empty"
            if receipt is None:
                return
```

In `_save_session`, replace:

```python
            try:
                await self._persist_close(
                    endpoint, ref, f"/sessions/{session_ref}/archive",
                    retention_days=self.control.config.workspace.session.persistence.retention_days,
                )
            except Exception as exc:
```

with:

```python
            try:
                with _storage_timer("persist", DOMAIN_SESSION) as timer:
                    await self._persist_close(
                        endpoint, ref, f"/sessions/{session_ref}/archive",
                        retention_days=self.control.config.workspace.session.persistence.retention_days,
                    )
                    timer["outcome"] = "ok"
            except Exception as exc:
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs/runtime
PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness timeout 600 \
  .venv/bin/pytest --import-mode=importlib tests/storage/test_config.py tests/harness/test_lifecycle.py \
  -q -p no:cacheprovider > /tmp/t1.log 2>&1; echo "EXIT=$?"; grep -E 'passed|failed|Error' /tmp/t1.log
```

Expected: `EXIT=0`, all passed.

- [ ] **Step 8: CHANGELOG**

In `CHANGELOG.md`, insert above `## [0.2.1] - 2026-10-31`:

```markdown
## [Unreleased]

### Added

- The control pod exports `ach_agent_storage_persist_seconds` and `ach_agent_storage_restore_seconds`
  histograms, labelled `domain` (`workspace` | `session`) and `outcome` (`ok` | `error`, and `empty` for a
  restore with nothing stored). A save includes the archive export and its retries; a restore includes
  streaming the archive into the sandbox.

### Changed

- The runtime uses path-style S3 addressing whenever `runtime.storage.s3.endpointUrl` is set.

```

- [ ] **Step 9: Commit**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
/usr/bin/git add runtime/src/ach_runtime/storage/config.py runtime/src/ach_runtime/observability/metrics.py \
  runtime/src/ach_runtime/harness/lifecycle.py runtime/tests/storage/test_config.py \
  runtime/tests/harness/test_lifecycle.py CHANGELOG.md
/usr/bin/git commit -F - <<'EOF'
feat(runtime): path-style endpoint and storage save/restore histograms

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
EOF
```

---

### Task 2: Chart: storage server per Agent namespace

**Files:**
- Create: `chart/files/storage/reconcile.sh`, `chart/files/storage/sync.sh`, `chart/files/storage/metrics.sh` (mode 0755)
- Create: `chart/templates/storage.yaml`
- Create: `chart/tests/storage_smoke.sh` (mode 0755)
- Modify: `chart/templates/_helpers.tpl`, `chart/templates/deployment.yaml`, `chart/values.yaml`, `chart/values.schema.json`, `chart/tests/render_test.sh`, `Makefile`
- Modify: `docs/install.md`, `runtime/docs/schemas/ach-workspace-contract-v1.md`, `CHANGELOG.md`, `CLAUDE.md`

`make chart-test` first fails on any uncommitted change under `chart/` (its drift check). Before a
commit, run the same checks directly ("chart checks"):

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
{ helm lint chart --set openshell.gateway=http://x:8080 &&
  PATH="$PWD/runtime/.venv/bin:$PATH" bash chart/tests/render_test.sh &&
  docker run --rm --entrypoint promtool -v "$PWD/chart:/chart:ro" -w /chart/tests prom/prometheus:v3.5.0 \
    test rules alerts.test.yaml; } > /tmp/chart.log 2>&1; echo "EXIT=$?"; grep -E 'FAIL|ok|PASS|SUCCESS|Error' /tmp/chart.log
```

and `make chart-test` once after the commit.

**Interfaces:**
- Consumes: Task 1's path-style client (the runtime talks to `http://ach-agent-storage:8333`).
- Produces (Task 3 and 4 rely on these names):
  - Helper `ach-agent.storage.active` renders `true` when `storage.enabled` and `runtime.storage.s3.bucket` are set; helpers `ach-agent.storage.selectorLabels` (`app.kubernetes.io/name: ach-agent-storage`, `app.kubernetes.io/instance: <release>`) and `ach-agent.storage.labels`.
  - In each namespace of `storage.namespaces`: StatefulSet/Service/ServiceAccount/AuthorizationPolicy `ach-agent-storage`, pod `ach-agent-storage-0`, Secret `ach-agent-storage-s3` (keys `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `identities.json`), ConfigMap `ach-agent-storage-scripts`.
  - Container ports: `server` exposes `s3` 8333 and `metrics` 9327 (SeaweedFS); `reconcile` exposes `reconcile` 9328 (gauges `ach_agent_storage_unsynced_files`, `ach_agent_storage_reconcile_ok_timestamp_seconds`).
  - Values: `storage.{enabled,namespaces,image,size,storageClass,evictThreshold,resources}`.
  - Make target `storage-smoke`.

- [ ] **Step 1: Write the scripts**

`chart/files/storage/reconcile.sh`:

```sh
#!/bin/sh
# Runs beside the SeaweedFS server in the ach-agent-storage pod. Points the server at the S3
# bucket and mounts it, then uploads the files the sync stream missed: a killed pod loses the
# change events of its last minute or two while the data stays on the volume. Exports two
# gauges on :9328.
# Env: BUCKET, AWS_REGION, REMOTE_ENDPOINT, RECONCILE_INTERVAL. AWS credentials come from the
# default chain (Pod Identity) or from AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
set -u
DIR="/buckets/${BUCKET}"
METRICS=/tmp/metrics

# weed shell exits non-zero when its command fails.
weed_sh() { printf '%s\n' "$2" | timeout "$1" weed shell -master=127.0.0.1:9333; }

export_metrics() {
  cat > "${METRICS}.tmp" <<EOF
# HELP ach_agent_storage_unsynced_files Files created more than 5 minutes ago that are not in S3.
# TYPE ach_agent_storage_unsynced_files gauge
ach_agent_storage_unsynced_files $1
# HELP ach_agent_storage_reconcile_ok_timestamp_seconds Last successful comparison with S3, or the loop start.
# TYPE ach_agent_storage_reconcile_ok_timestamp_seconds gauge
ach_agent_storage_reconcile_ok_timestamp_seconds $2
EOF
  mv "${METRICS}.tmp" "$METRICS"
}

export_metrics 0 "$(date +%s)"
nc -lk -p 9328 -e /scripts/metrics.sh &

i=0
until curl -s -o /dev/null -m 2 http://127.0.0.1:8888/; do
  i=$((i + 1))
  [ "$i" -lt 90 ] || { echo "FAIL: filer not up after 180s" >&2; exit 1; }
  sleep 2
done

# The wasabi type uses the default AWS credential chain; the s3 type signs anonymously when it has no keys.
weed_sh 60 "remote.configure -name=aws -type=wasabi -wasabi.endpoint=${REMOTE_ENDPOINT} -wasabi.region=${AWS_REGION}" >/dev/null || exit 1
if ! weed_sh 60 "remote.mount" | grep -qF "\"${DIR}\":"; then
  weed_sh 1800 "remote.mount -dir=${DIR} -remote=aws/${BUCKET} -nonempty" || exit 1
fi
touch /tmp/mounted
echo "mounted ${DIR} on aws/${BUCKET} at ${REMOTE_ENDPOINT}"

weed_sh 3600 "remote.copy.local -dir=${DIR}" >/dev/null ||
  echo "start-up reconcile failed; retrying every ${RECONCILE_INTERVAL}s" >&2

while true; do
  sleep "$RECONCILE_INTERVAL"
  # Younger files are normally still on their way through filer.remote.sync.
  if out=$(weed_sh 1800 "remote.copy.local -dir=${DIR} -minAge=300 -dryRun=true"); then
    n=$(printf '%s\n' "$out" | sed -n 's/^Files to copy: //p')
    export_metrics "${n:-0}" "$(date +%s)"
    if [ "${n:-0}" -gt 0 ]; then
      echo "uploading ${n} files the sync stream missed"
      weed_sh 3600 "remote.copy.local -dir=${DIR} -minAge=300" >/dev/null || echo "reconcile upload failed" >&2
    fi
  else
    echo "reconcile: cannot compare with S3; retrying in ${RECONCILE_INTERVAL}s" >&2
  fi
done
```

`chart/files/storage/sync.sh`:

```sh
#!/bin/sh
# Writes local changes back to S3 once reconcile.sh has mounted the bucket. filer.remote.sync
# keeps its offset in the filer and resumes from it after a restart.
i=0
until [ -f /tmp/mounted ]; do
  i=$((i + 1))
  [ "$i" -lt 900 ] || { echo "FAIL: bucket not mounted after 1800s" >&2; exit 1; }
  sleep 2
done
exec weed filer.remote.sync -filer=127.0.0.1:8888 -dir="/buckets/${BUCKET}"
```

`chart/files/storage/metrics.sh`:

```sh
#!/bin/sh
# Answers the one HTTP request on the connection nc hands over with the gauges reconcile.sh writes.
# Reads the whole request first: closing with unread bytes resets the connection.
cr=$(printf '\r')
while IFS= read -r line; do
  [ -z "${line%"$cr"}" ] && break
done
printf 'HTTP/1.0 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n\r\n'
cat /tmp/metrics
```

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs && chmod 0755 chart/files/storage/*.sh
```

- [ ] **Step 2: Write the smoke test (docker, the scripts against a stand-in S3)**

`chart/tests/storage_smoke.sh`:

```bash
#!/usr/bin/env bash
# Runs the storage server's three containers as the chart wires them (shared /tmp, non-root,
# read-only root) in front of a second SeaweedFS standing in for S3, then checks: create-only
# writes, replication to the stand-in, eviction of a replicated object (SeaweedFS 4.48 never
# marks objects written through the server as replicated) and read-through, and the reconcile
# gauges. The docker volume reports the host disk, so the built-in evictor gets a 0.99 threshold
# here to keep it out of the explicit uncache check. Needs docker and runtime/.venv (boto3).
# Run before changing storage.image.
set -euo pipefail
cd "$(dirname "$0")/../.."
IMG=$(sed -n 's/^  image: *//p' chart/values.yaml | grep seaweedfs)
N=ach-storage-smoke
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  docker rm -f smoke-remote smoke-ss smoke-sync smoke-loop >/dev/null 2>&1 || true
  docker volume rm -f smoke-tmp smoke-data >/dev/null 2>&1 || true
  docker network rm "$N" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
retry() { # retry <tries> <command...>: every 2 s
  local n=$1; shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}

docker network create "$N" >/dev/null
docker run -d --name smoke-remote --network "$N" "$IMG" server -dir=/data -s3 -ip=smoke-remote -ip.bind=0.0.0.0 >/dev/null
retry 60 docker exec smoke-remote curl -s -o /dev/null -m 2 http://127.0.0.1:8333/ || fail "stand-in S3 not up"
printf '%s\n' 's3.configure -user=remote -access_key=remote -secret_key=remote-secret -actions=Admin -apply' \
  's3.bucket.create -name smoke' | docker exec -i smoke-remote weed shell -master=127.0.0.1:9333 >/dev/null

docker volume create smoke-tmp >/dev/null
docker volume create smoke-data >/dev/null
docker run --rm -v smoke-tmp:/tmp -v smoke-data:/data --entrypoint chown "$IMG" 1000:1000 /tmp /data
ident='{"identities":[{"name":"control","credentials":[{"accessKey":"ctl","secretKey":"ctl-secret"}],"actions":["Read","Write","List","Tagging"]}]}'
common=(--user 1000:1000 --read-only -v smoke-tmp:/tmp -e HOME=/tmp -e BUCKET=smoke -e AWS_REGION=us-east-1
  -e REMOTE_ENDPOINT=http://smoke-remote:8333 -e RECONCILE_INTERVAL=15
  -e AWS_ACCESS_KEY_ID=remote -e AWS_SECRET_ACCESS_KEY=remote-secret)
docker run -d --name smoke-ss --network "$N" "${common[@]}" -v smoke-data:/data \
  --entrypoint sh "$IMG" -c "mkdir -p /tmp/s3 && printf '%s' '$ident' > /tmp/s3/identities.json && exec /usr/bin/weed -logtostderr=true server \
  -dir=/data -filer -s3 -s3.config=/tmp/s3/identities.json -ip=127.0.0.1 -ip.bind=0.0.0.0 \
  -metricsPort=9327 -volume.max=0 -master.volumeSizeLimitMB=256 -filer.remoteCacheEvictThreshold=0.99 \
  -volume.allowUntrustedRemoteEndpoints -filer.allowUntrustedRemoteEndpoints -s3.allowUntrustedRemoteEndpoints" >/dev/null
docker run -d --name smoke-loop --network container:smoke-ss "${common[@]}" -v "$PWD/chart/files/storage:/scripts:ro" \
  --entrypoint sh "$IMG" /scripts/reconcile.sh >/dev/null
docker run -d --name smoke-sync --network container:smoke-ss "${common[@]}" -v "$PWD/chart/files/storage:/scripts:ro" \
  --entrypoint sh "$IMG" /scripts/sync.sh >/dev/null
retry 90 docker exec smoke-loop test -f /tmp/mounted || { docker logs smoke-loop; fail "bucket never mounted"; }

ip=$(docker inspect smoke-ss -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
runtime/.venv/bin/python - "$ip" <<'PY'
import sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="ctl",
                 aws_secret_access_key="ctl-secret", region_name="us-east-1",
                 config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
key = "ach/ns/uid/ws/registry.json"
c.put_object(Bucket="smoke", Key=key, Body=b'{"v": 1}', IfNoneMatch="*")
try:
    c.put_object(Bucket="smoke", Key=key, Body=b'{"v": 2}', IfNoneMatch="*")
    raise SystemExit("FAIL: a second create-only put succeeded")
except c.exceptions.ClientError as exc:
    assert exc.response["Error"]["Code"] in ("PreconditionFailed", "412"), exc.response
bad = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="nobody",
                   aws_secret_access_key="nope", region_name="us-east-1",
                   config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
try:
    bad.get_object(Bucket="smoke", Key=key)
    raise SystemExit("FAIL: an unknown key read an object")
except bad.exceptions.ClientError:
    pass
print("create-only put and auth ok")
PY

# grep without -q reads all input: an early exit would fail the pipeline under pipefail.
in_remote() { printf 'fs.ls /buckets/smoke/ach/ns/uid/ws\n' | docker exec -i smoke-remote weed shell -master=127.0.0.1:9333 | grep -x registry.json >/dev/null; }
retry 60 in_remote || { docker logs smoke-sync; fail "object never reached the stand-in S3"; }
uncached() { printf 'remote.uncache -dir=/buckets/smoke\n' | docker exec -i smoke-ss weed shell -master=127.0.0.1:9333 | grep 'registry.json ... Done' >/dev/null; }
retry 30 uncached || fail "the replicated object cannot be evicted"
runtime/.venv/bin/python - "$ip" <<'PY'
import sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="ctl",
                 aws_secret_access_key="ctl-secret", region_name="us-east-1",
                 config=Config(s3={"addressing_style": "path"}))
assert c.get_object(Bucket="smoke", Key="ach/ns/uid/ws/registry.json")["Body"].read() == b'{"v": 1}'
print("read-through after eviction ok")
PY

gauges() { docker exec smoke-ss curl -s -m 5 http://127.0.0.1:9328/metrics; }
start=$(gauges | sed -n 's/^ach_agent_storage_reconcile_ok_timestamp_seconds //p')
[ -n "$start" ] || fail "no reconcile gauges on :9328"
fresh() { [ "$(gauges | sed -n 's/^ach_agent_storage_reconcile_ok_timestamp_seconds //p')" -gt "$start" ]; }
retry 30 fresh || { docker logs smoke-loop; fail "the reconcile loop never compared with S3"; }
gauges | grep -x 'ach_agent_storage_unsynced_files 0' >/dev/null || fail "unexpected unsynced files: $(gauges)"
docker exec smoke-ss curl -s -m 5 http://127.0.0.1:9327/metrics | grep '^SeaweedFS_volumeServer_resource' >/dev/null ||
  fail "no SeaweedFS metrics on :9327"
echo PASS
```

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs && chmod 0755 chart/tests/storage_smoke.sh
```

- [ ] **Step 3: Add the make target and run the smoke test to see it fail**

In `Makefile`, add `storage-smoke` to `.PHONY` and, after the `chart-test` target:

```make
storage-smoke: ## run the storage server's containers against a stand-in S3 in docker (before changing storage.image)
	bash chart/tests/storage_smoke.sh
```

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
timeout 600 make storage-smoke > /tmp/smoke.log 2>&1; echo "EXIT=$?"; grep -E 'FAIL|PASS' /tmp/smoke.log
```

Expected: `EXIT=2`: `chart/values.yaml` has no SeaweedFS `image:` line yet, so the `IMG=` pipeline fails.

- [ ] **Step 4: Values and schema**

Append to `chart/values.yaml`:

```yaml

storage:
  # One storage server per Agent namespace in front of runtime.storage.s3: control pods save to it and it
  # replicates to the bucket in the background. Runs when enabled and runtime.storage.s3.bucket is set; then
  # namespaces (the Agent namespaces) and runtime.storage.s3.region are required. Credentials for the bucket:
  # Pod Identity on the ServiceAccount ach-agent-storage, or runtime.storage.s3.credentialsSecret.
  enabled: true
  namespaces: []
  # SeaweedFS 4.51 or newer: 4.48 never marks objects written through the server as replicated. Run
  # make storage-smoke before changing it.
  image: chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8
  # The volume holds a cache of the bucket plus whatever S3 does not have yet.
  size: 50Gi
  storageClass: ""
  # Disk usage above which data already in S3 is dropped from the volume, oldest first.
  evictThreshold: 0.8
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits: {memory: 1Gi}
```

In `chart/values.schema.json`, add after the `"admissionPolicy"` property:

```json
    "storage": {
      "type": "object",
      "properties": {
        "enabled": {"type": "boolean"},
        "namespaces": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "image": {"type": "string", "minLength": 1},
        "size": {"type": "string", "minLength": 1},
        "storageClass": {"type": "string"},
        "evictThreshold": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "resources": {"type": "object"}
      }
    },
```

- [ ] **Step 5: Helpers, validation and the operator env switch**

Append to `chart/templates/_helpers.tpl`:

```
{{/* The storage server runs when it is enabled and there is a bucket to replicate to. */}}
{{- define "ach-agent.storage.active" -}}
{{- if and .Values.storage.enabled .Values.runtime.storage.s3.bucket }}true{{ end -}}
{{- end -}}

{{- define "ach-agent.storage.selectorLabels" -}}
app.kubernetes.io/name: ach-agent-storage
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ach-agent.storage.labels" -}}
{{ include "ach-agent.storage.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
```

In the same file, inside `ach-agent.validate`, before its final `{{- end -}}`:

```
{{- if eq (include "ach-agent.storage.active" .) "true" -}}
{{- if not .Values.storage.namespaces -}}
{{- fail "storage.namespaces must list the Agent namespaces that get a storage server (or set storage.enabled=false)" -}}
{{- end -}}
{{- if not .Values.runtime.storage.s3.region -}}
{{- fail "runtime.storage.s3.region is required when the storage server is enabled" -}}
{{- end -}}
{{- end -}}
```

In `chart/templates/deployment.yaml`, after `{{- $parent := .Values.route.parent -}}` add:

```
{{- $storage := eq (include "ach-agent.storage.active" .) "true" -}}
```

and replace the two env values:

```yaml
            - name: ACH_AGENT_STORAGE_S3_ENDPOINT_URL
              value: {{ ternary "http://ach-agent-storage:8333" $s3.endpointUrl $storage | quote }}
```

```yaml
            - name: ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET
              value: {{ ternary "ach-agent-storage-s3" $s3.credentialsSecret $storage | quote }}
```

- [ ] **Step 6: Write the failing render tests**

In `chart/tests/render_test.sh`, in the first python block (default render), after `cr = one("ClusterRole")`'s loop add:

```python
assert not [d for d in docs if d["kind"] in ("StatefulSet", "AuthorizationPolicy")], "storage renders without a bucket"
```

Then, before the `on=(...)` line, add:

```bash
s3=(--set runtime.storage.s3.bucket=ach-bucket --set runtime.storage.s3.region=eu-west-1)
helm template t . "${base[@]}" "${s3[@]}" >/dev/null 2>&1 && fail "renders a storage server without storage.namespaces"
helm template t . "${base[@]}" --set runtime.storage.s3.bucket=ach-bucket --set 'storage.namespaces={ach-agent}' \
  >/dev/null 2>&1 && fail "renders a storage server without a region"

out=$(helm template t . "${base[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent,team-b}')
python3 - "$out" <<'PY'
import base64, json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
def of(kind, ns=None):
    return [d for d in docs if d["kind"] == kind and (ns is None or d["metadata"].get("namespace") == ns)]
dep, = of("Deployment")
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://ach-agent-storage:8333", env
assert env["ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"] == "ach-agent-storage-s3", env
assert env["ACH_AGENT_STORAGE_S3_BUCKET"] == "ach-bucket", env
for kind in ("StatefulSet", "Service", "Secret", "ConfigMap", "ServiceAccount", "AuthorizationPolicy"):
    got = sorted(d["metadata"]["namespace"] for d in of(kind) if d["metadata"]["name"].startswith("ach-agent-storage"))
    assert got == ["ach-agent", "team-b"], (kind, got)
sts, = of("StatefulSet", "ach-agent")
pod = sts["spec"]["template"]["spec"]
assert sts["spec"]["replicas"] == 1 and sts["spec"]["serviceName"] == "ach-agent-storage"
assert pod["serviceAccountName"] == "ach-agent-storage"
assert pod["securityContext"]["runAsNonRoot"] and pod["securityContext"]["runAsUser"] == 1000
c = {x["name"]: x for x in pod["containers"]}
assert sorted(c) == ["reconcile", "server", "sync"], sorted(c)
for x in c.values():
    assert x["image"] == "chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8", x["image"]
    sc = x["securityContext"]
    assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
    e = {v["name"]: v for v in x["env"]}
    assert e["BUCKET"]["value"] == "ach-bucket" and e["AWS_REGION"]["value"] == "eu-west-1", e
    assert e["REMOTE_ENDPOINT"]["value"] == "https://s3.eu-west-1.amazonaws.com", e
    assert "AWS_ACCESS_KEY_ID" not in e, "without credentialsSecret the default AWS chain (Pod Identity) applies"
args = c["server"]["args"]
for flag in ("-filer.remoteCacheEvictThreshold=0.8", "-s3.config=/etc/seaweedfs/identities.json",
             "-ip.bind=0.0.0.0", "-filer.allowUntrustedRemoteEndpoints"):
    assert flag in args, (flag, args)
assert {p["name"]: p["containerPort"] for p in c["server"]["ports"]} == {"s3": 8333, "metrics": 9327}
assert {p["name"]: p["containerPort"] for p in c["reconcile"]["ports"]} == {"reconcile": 9328}
assert sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "50Gi"
sec, = [d for d in of("Secret", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage-s3"]
data = {k: base64.b64decode(v).decode() for k, v in sec["data"].items()}
ident = json.loads(data["identities.json"])["identities"][0]
assert ident["credentials"][0] == {"accessKey": data["AWS_ACCESS_KEY_ID"], "secretKey": data["AWS_SECRET_ACCESS_KEY"]}
assert "Admin" not in ident["actions"], ident
svc, = [d for d in of("Service", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage"]
assert [p["port"] for p in svc["spec"]["ports"]] == [8333]
ap, = of("AuthorizationPolicy", "ach-agent")
rules = ap["spec"]["rules"]
assert rules[0]["from"][0]["source"]["principals"] == ["cluster.local/ns/ach-agent/sa/ach-agent-control*"], rules
assert rules[0]["to"][0]["operation"]["ports"] == ["8333"], rules
assert "from" not in rules[1] and rules[1]["to"][0]["operation"]["ports"] == ["9327", "9328"], rules
cm, = [d for d in of("ConfigMap", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage-scripts"]
assert sorted(cm["data"]) == ["metrics.sh", "reconcile.sh", "sync.sh"]
print("storage ok")
PY

out=$(helm template t . "${base[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent}' \
  --set runtime.storage.s3.endpointUrl=http://minio.minio:9000 --set runtime.storage.s3.credentialsSecret=s3-keys)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
dep, = [d for d in docs if d["kind"] == "Deployment"]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://ach-agent-storage:8333", env
sts, = [d for d in docs if d["kind"] == "StatefulSet"]
for x in sts["spec"]["template"]["spec"]["containers"]:
    e = {v["name"]: v for v in x["env"]}
    assert e["REMOTE_ENDPOINT"]["value"] == "http://minio.minio:9000", e
    assert e["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {"name": "s3-keys", "key": "AWS_ACCESS_KEY_ID"}, e
    assert e["AWS_SESSION_TOKEN"]["valueFrom"]["secretKeyRef"]["optional"] is True, e
print("storage custom endpoint ok")
PY

out=$(helm template t . "${base[@]}" "${s3[@]}" --set storage.enabled=false \
  --set runtime.storage.s3.endpointUrl=http://minio.minio:9000 --set runtime.storage.s3.credentialsSecret=s3-keys)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
assert not [d for d in docs if d["kind"] == "StatefulSet"]
dep, = [d for d in docs if d["kind"] == "Deployment"]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://minio.minio:9000", env
assert env["ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"] == "s3-keys", env
print("storage disabled ok")
PY
```

Run the chart checks (top of this task). Expected: `EXIT=1`; `render ok` prints, then the first storage check fails (no StatefulSet rendered yet).

- [ ] **Step 7: The storage template**

`chart/templates/storage.yaml`:

```yaml
{{- if eq (include "ach-agent.storage.active" .) "true" }}
{{- $s3 := .Values.runtime.storage.s3 }}
{{- $st := .Values.storage }}
{{- $endpoint := $s3.endpointUrl | default (printf "https://s3.%s.amazonaws.com" $s3.region) }}
{{- $scripts := dict "reconcile.sh" (.Files.Get "files/storage/reconcile.sh") "sync.sh" (.Files.Get "files/storage/sync.sh") "metrics.sh" (.Files.Get "files/storage/metrics.sh") }}
{{- $env := list (dict "name" "HOME" "value" "/tmp") (dict "name" "BUCKET" "value" $s3.bucket) (dict "name" "AWS_REGION" "value" $s3.region) (dict "name" "REMOTE_ENDPOINT" "value" $endpoint) (dict "name" "RECONCILE_INTERVAL" "value" "600") }}
{{- with $s3.credentialsSecret }}
{{- $env = concat $env (list (dict "name" "AWS_ACCESS_KEY_ID" "valueFrom" (dict "secretKeyRef" (dict "name" . "key" "AWS_ACCESS_KEY_ID"))) (dict "name" "AWS_SECRET_ACCESS_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" . "key" "AWS_SECRET_ACCESS_KEY"))) (dict "name" "AWS_SESSION_TOKEN" "valueFrom" (dict "secretKeyRef" (dict "name" . "key" "AWS_SESSION_TOKEN" "optional" true)))) }}
{{- end }}
{{- $containerSecurity := dict "allowPrivilegeEscalation" false "readOnlyRootFilesystem" true "capabilities" (dict "drop" (list "ALL")) }}
{{- range $ns := $st.namespaces }}
{{- /* The control pods' S3 key: generated once per namespace, kept across upgrades. */}}
{{- $old := lookup "v1" "Secret" $ns "ach-agent-storage-s3" }}
{{- $ak := randAlphaNum 20 }}
{{- $sk := randAlphaNum 40 }}
{{- if $old }}
{{- $ak = index $old.data "AWS_ACCESS_KEY_ID" | b64dec }}
{{- $sk = index $old.data "AWS_SECRET_ACCESS_KEY" | b64dec }}
{{- end }}
---
apiVersion: v1
kind: Secret
metadata:
  name: ach-agent-storage-s3
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
type: Opaque
data:
  AWS_ACCESS_KEY_ID: {{ $ak | b64enc }}
  AWS_SECRET_ACCESS_KEY: {{ $sk | b64enc }}
  identities.json: {{ dict "identities" (list (dict "name" "control" "credentials" (list (dict "accessKey" $ak "secretKey" $sk)) "actions" (list "Read" "Write" "List" "Tagging"))) | toJson | b64enc }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ach-agent-storage-scripts
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
data:
  {{- toYaml $scripts | nindent 2 }}
---
# EKS Pod Identity binds the bucket's IAM role to this ServiceAccount.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ach-agent-storage
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
---
apiVersion: v1
kind: Service
metadata:
  name: ach-agent-storage
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
spec:
  selector:
    {{- include "ach-agent.storage.selectorLabels" $ | nindent 4 }}
  ports:
    - {name: s3, port: 8333, targetPort: s3}
---
# One replica on one RWO volume: two pods never share the cache. No PodDisruptionBudget, so a
# drain moves it; saves retry through the short outage.
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ach-agent-storage
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
spec:
  replicas: 1
  serviceName: ach-agent-storage
  selector:
    matchLabels:
      {{- include "ach-agent.storage.selectorLabels" $ | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "ach-agent.storage.selectorLabels" $ | nindent 8 }}
      annotations:
        checksum/scripts: {{ toJson $scripts | sha256sum }}
        checksum/s3-key: {{ $ak | sha256sum }}
    spec:
      serviceAccountName: ach-agent-storage
      # SIGTERM lets filer.remote.sync save its offset; a kill is covered by the reconcile loop.
      terminationGracePeriodSeconds: 60
      {{- with $.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      volumes:
        # Shared /tmp: the server's unix sockets, the mount marker and the reconcile gauges.
        - {name: tmp, emptyDir: {}}
        - {name: scripts, configMap: {name: ach-agent-storage-scripts, defaultMode: 0555}}
        - name: identities
          secret:
            secretName: ach-agent-storage-s3
            items: [{key: identities.json, path: identities.json}]
      containers:
        - name: server
          image: {{ $st.image }}
          command: [/usr/bin/weed]
          args:
            - -logtostderr=true
            - server
            - -dir=/data
            - -filer
            - -s3
            - -s3.config=/etc/seaweedfs/identities.json
            # All ports bind to every interface: bound to loopback, the volume server also dials S3
            # from loopback and read-through fails. The AuthorizationPolicy admits S3 and metrics only.
            - -ip=127.0.0.1
            - -ip.bind=0.0.0.0
            - -metricsPort=9327
            - -volume.max=0
            - -master.volumeSizeLimitMB=256
            - -filer.remoteCacheEvictThreshold={{ $st.evictThreshold }}
            # The remote endpoint is set only by this chart; an S3 VPC endpoint or an in-cluster S3
            # resolves to private addresses, which SeaweedFS rejects without these.
            - -volume.allowUntrustedRemoteEndpoints
            - -filer.allowUntrustedRemoteEndpoints
            - -s3.allowUntrustedRemoteEndpoints
          env:
            {{- toYaml $env | nindent 12 }}
          ports:
            - {name: s3, containerPort: 8333}
            - {name: metrics, containerPort: 9327}
          readinessProbe:
            tcpSocket: {port: s3}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tmp, mountPath: /tmp}
            - {name: identities, mountPath: /etc/seaweedfs, readOnly: true}
          securityContext:
            {{- toYaml $containerSecurity | nindent 12 }}
          resources:
            {{- toYaml $st.resources | nindent 12 }}
        - name: sync
          image: {{ $st.image }}
          command: [/bin/sh, /scripts/sync.sh]
          env:
            {{- toYaml $env | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: scripts, mountPath: /scripts, readOnly: true}
          securityContext:
            {{- toYaml $containerSecurity | nindent 12 }}
        - name: reconcile
          image: {{ $st.image }}
          command: [/bin/sh, /scripts/reconcile.sh]
          env:
            {{- toYaml $env | nindent 12 }}
          ports:
            - {name: reconcile, containerPort: 9328}
          # The Service gets the pod only once the bucket is mounted.
          readinessProbe:
            exec: {command: [sh, -c, "test -f /tmp/mounted"]}
            periodSeconds: 5
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: scripts, mountPath: /scripts, readOnly: true}
          securityContext:
            {{- toYaml $containerSecurity | nindent 12 }}
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        {{- with $st.storageClass }}
        storageClassName: {{ . }}
        {{- end }}
        resources:
          requests:
            storage: {{ $st.size }}
---
# Control pods (ServiceAccounts ach-agent-control*) may use S3; sandboxes run elsewhere and are
# denied. Metrics are open to the scraper.
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: ach-agent-storage
  namespace: {{ $ns }}
  labels:
    {{- include "ach-agent.storage.labels" $ | nindent 4 }}
spec:
  selector:
    matchLabels:
      {{- include "ach-agent.storage.selectorLabels" $ | nindent 6 }}
  action: ALLOW
  rules:
    - from:
        - source:
            principals: ["cluster.local/ns/{{ $ns }}/sa/ach-agent-control*"]
      to:
        - operation: {ports: ["8333"]}
    - to:
        - operation: {ports: ["9327", "9328"]}
{{- end }}
{{- end }}
```

- [ ] **Step 8: Run render tests and the smoke test to verify they pass**

Run the chart checks (top of this task), then:

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
timeout 600 make storage-smoke > /tmp/smoke.log 2>&1; echo "EXIT=$?"; grep -E 'FAIL|ok|PASS' /tmp/smoke.log
```

Expected: both `EXIT=0`; render prints `storage ok`, `storage custom endpoint ok`, `storage disabled ok`, `PASS`; promtool `SUCCESS`; the smoke prints `create-only put and auth ok`, `read-through after eviction ok`, `PASS`.

- [ ] **Step 9: Docs**

`docs/install.md`: insert after the paragraph that ends "`admissionPolicy.enabled=false` disables it)." and before `### Observability (optional)`:

````markdown
### Storage server

With `runtime.storage.s3.bucket` set and `storage.enabled` (the default), the chart runs one storage
server, `ach-agent-storage` (SeaweedFS), in every namespace listed in `storage.namespaces`. Control pods
in that namespace save and restore workspaces and sessions through it; it keeps them on its volume and
replicates them to the bucket in the background. A save is done once the storage server has it.

```bash
helm upgrade --install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version <version> \
  -n ach-agent-system \
  --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080 \
  --set runtime.storage.s3.bucket=<bucket> --set runtime.storage.s3.region=<region> \
  --set 'storage.namespaces={ach-agent}'
```

| Value | Default | Notes |
|-------|---------|-------|
| `storage.enabled` | `true` | `false` keeps control pods on S3 directly, with their own credentials. |
| `storage.namespaces` | `[]` | The Agent namespaces; required when a bucket is set. An Agent elsewhere with persistence or artifacts enabled waits for the Secret `ach-agent-storage-s3`. |
| `storage.size`, `storage.storageClass` | `50Gi`, cluster default | A cache of the bucket plus what S3 does not have yet: size it for the backlog of an S3 outage. A StatefulSet cannot resize its claim; grow the PVC by hand. |
| `storage.evictThreshold` | `0.8` | Above this disk usage the server drops data already in S3, oldest first; a later read fetches it again. Data not yet in S3 is never dropped. |
| `storage.image` | SeaweedFS 4.51, pinned by digest | 4.51 or newer: 4.48 never marks objects written through the server as replicated, so it never evicts them. Run `make storage-smoke` before changing it. |

- **S3.** The storage server replicates to `runtime.storage.s3.bucket` in `region`, at `endpointUrl`
  (default `https://s3.<region>.amazonaws.com`). It is the only component with AWS credentials, from the
  default AWS chain: on EKS, one Pod Identity association per namespace for the ServiceAccount
  `ach-agent-storage`, with the bucket policy the control ServiceAccount had; remove the association on the
  control ServiceAccount after the switch. `runtime.storage.s3.credentialsSecret` instead names a Secret
  with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (optionally `AWS_SESSION_TOKEN`) in each listed
  namespace.
- **Control pods.** The operator points them at `http://ach-agent-storage:8333` with the S3 key the chart
  generates into the Secret `ach-agent-storage-s3`. Helm `lookup` keeps the key across upgrades, so install
  with `helm` or Flux, not through a `helm template` pipeline. An Istio AuthorizationPolicy admits only
  ServiceAccounts named `ach-agent-control*` to port 8333 (the default per-Agent name and the usual profile
  name); an Agent whose profile sets another `control.serviceAccountName` cannot reach it.
- **Existing data.** Objects already in the bucket stay readable. The first start reads the bucket's
  object list, which takes a while for a large bucket.
- **Failures.** While the storage pod restarts, saves retry within `workspace.shutdownTimeoutSeconds` and
  a failed restore is retried by the next message. While S3 is unreachable, saves still succeed but each
  new object waits on S3 (about 30 s when the connection is refused, about 90 s when the endpoint does not
  resolve) and the backlog grows on the volume. A killed pod's last minute or two of changes reach S3
  through the reconcile loop, which runs at start and every 10 minutes. Losing the volume loses what S3
  does not have yet.

````

`runtime/docs/schemas/ach-workspace-contract-v1.md`: insert a new paragraph right after the paragraph at line 246 (it begins "La implementación usa un contenedor Channels + Harness"):

```markdown
With the chart's storage server enabled, the Harness's S3 endpoint is the storage server of its namespace (`http://ach-agent-storage:8333`, path-style, a static key the chart generates). It is an S3 endpoint, not a new protocol, and it alone holds the AWS credentials; it replicates to the configured bucket in the background. A write it acknowledges counts as stored, and it evaluates `If-None-Match`/`If-Match` against its own state, so the registry CAS and fencing hold because every writer of the namespace goes through it. This reverses the "Harness owns the S3 client, no Storage broker" choice of D1 for that deployment; without the storage server the Harness talks to S3 directly as above.
```

`CHANGELOG.md`, under `## [Unreleased]`: append to `### Added`:

```markdown
- Storage server: with `runtime.storage.s3.bucket` set and `storage.enabled` (the default), the chart runs a
  SeaweedFS 4.51 storage server, `ach-agent-storage`, in each namespace of `storage.namespaces`. Control pods
  save to it and it replicates to the bucket in the background, so a save no longer waits for S3. Only the
  storage server holds AWS credentials. `make storage-smoke` checks a SeaweedFS version in docker. See
  `docs/install.md`.
```

and append to `### Changed`:

```markdown
- **Breaking:** with a bucket configured, the chart requires `storage.namespaces` (the Agent namespaces) and
  `runtime.storage.s3.region`, or `storage.enabled=false` to keep control pods on S3 directly. The bucket's
  Pod Identity association moves from the control ServiceAccount to `<namespace>/ach-agent-storage`.
```

`CLAUDE.md` (repo root): change the chart line to:

```markdown
- `chart/` Helm chart (operator, CRDs, RBAC, Secret admission policy, a SeaweedFS storage server per Agent namespace with its scripts in `chart/files/storage/`); `make chart-test`, `make storage-smoke` (docker).
```

- [ ] **Step 10: Commit**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
/usr/bin/git add chart/files/storage chart/templates/storage.yaml chart/templates/_helpers.tpl \
  chart/templates/deployment.yaml chart/values.yaml chart/values.schema.json chart/tests/render_test.sh \
  chart/tests/storage_smoke.sh Makefile docs/install.md runtime/docs/schemas/ach-workspace-contract-v1.md \
  CHANGELOG.md CLAUDE.md
/usr/bin/git commit -F - <<'EOF'
feat(chart): run a SeaweedFS storage server per Agent namespace

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
EOF
timeout 600 make chart-test > /tmp/chart.log 2>&1; echo "EXIT=$?"; grep -E 'FAIL|ok|PASS|SUCCESS' /tmp/chart.log
```

Expected: `EXIT=0` (no drift: `make chart-sync` regenerates only `chart/crds` and `clusterrole.yaml`, which this task does not touch).

---

### Task 3: Chart: storage server scrape and alerts

**Files:**
- Modify: `chart/templates/podmonitor.yaml`, `chart/alerts/ach-agent.rules.yaml`, `chart/tests/alerts.test.yaml`, `chart/tests/render_test.sh`
- Modify: `docs/install.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: from Task 2, helpers `ach-agent.storage.active`, `ach-agent.storage.selectorLabels`, `ach-agent.storage.labels`; container ports `metrics` (9327) and `reconcile` (9328); gauges `ach_agent_storage_unsynced_files`, `ach_agent_storage_reconcile_ok_timestamp_seconds`; SeaweedFS's `SeaweedFS_volumeServer_resource{type="used"|"all"}`.
- Produces: PodMonitor `ach-agent-storage`; alerts `ACHAgentStorageDown`, `ACHAgentStorageDiskHigh`, `ACHAgentStorageSyncLagging` in the `ach-agent` PrometheusRule.

- [ ] **Step 1: Write the failing tests**

Append to `chart/tests/alerts.test.yaml` (under `tests:`):

```yaml
  - name: storage server down fires after five minutes without a ready replica
    interval: 30s
    input_series:
      - series: 'kube_statefulset_status_replicas_ready{namespace="ach-agent", statefulset="ach-agent-storage"}'
        values: '0x20'
      - series: 'kube_statefulset_status_replicas_ready{namespace="team-b", statefulset="ach-agent-storage"}'
        values: '1x20'
    alert_rule_test:
      - eval_time: 4m
        alertname: ACHAgentStorageDown
        exp_alerts: []
      - eval_time: 6m
        alertname: ACHAgentStorageDown
        exp_alerts:
          - exp_labels:
              namespace: ach-agent
              severity: critical
            exp_annotations:
              summary: ACH agent storage server is down in namespace ach-agent
              description: The storage server ach-agent-storage in namespace ach-agent has had no ready pod for 5 minutes. Workspace and session saves and restores in that namespace fail until it is back.

  - name: storage disk high fires when usage stays above 85 percent
    interval: 30s
    input_series:
      - series: 'SeaweedFS_volumeServer_resource{namespace="ach-agent", pod="ach-agent-storage-0", name="/data", type="used"}'
        values: '90x40'
      - series: 'SeaweedFS_volumeServer_resource{namespace="ach-agent", pod="ach-agent-storage-0", name="/data", type="all"}'
        values: '100x40'
      - series: 'SeaweedFS_volumeServer_resource{namespace="team-b", pod="ach-agent-storage-0", name="/data", type="used"}'
        values: '50x40'
      - series: 'SeaweedFS_volumeServer_resource{namespace="team-b", pod="ach-agent-storage-0", name="/data", type="all"}'
        values: '100x40'
    alert_rule_test:
      - eval_time: 14m
        alertname: ACHAgentStorageDiskHigh
        exp_alerts: []
      - eval_time: 16m
        alertname: ACHAgentStorageDiskHigh
        exp_alerts:
          - exp_labels:
              namespace: ach-agent
              pod: ach-agent-storage-0
              severity: warning
            exp_annotations:
              summary: ACH agent storage volume is 90% full in namespace ach-agent
              description: The storage server in namespace ach-agent cannot evict below storage.evictThreshold, so most of its volume holds data S3 does not have yet. Check S3 reachability and the sync container; grow the volume if the backlog is legitimate.

  - name: storage sync lag fires on old unsynced files or a stale reconcile
    interval: 30s
    input_series:
      - series: 'ach_agent_storage_unsynced_files{namespace="ach-agent", pod="ach-agent-storage-0"}'
        values: '3x100'
      - series: 'ach_agent_storage_reconcile_ok_timestamp_seconds{namespace="ach-agent", pod="ach-agent-storage-0"}'
        values: '0+30x100'
      - series: 'ach_agent_storage_unsynced_files{namespace="team-b", pod="ach-agent-storage-0"}'
        values: '0x100'
      - series: 'ach_agent_storage_reconcile_ok_timestamp_seconds{namespace="team-b", pod="ach-agent-storage-0"}'
        values: '0x100'
      - series: 'ach_agent_storage_unsynced_files{namespace="team-c", pod="ach-agent-storage-0"}'
        values: '0x100'
      - series: 'ach_agent_storage_reconcile_ok_timestamp_seconds{namespace="team-c", pod="ach-agent-storage-0"}'
        values: '0+30x100'
    alert_rule_test:
      - eval_time: 14m
        alertname: ACHAgentStorageSyncLagging
        exp_alerts: []
      - eval_time: 16m
        alertname: ACHAgentStorageSyncLagging
        exp_alerts:
          - exp_labels:
              namespace: ach-agent
              severity: warning
            exp_annotations:
              summary: ACH agent storage in namespace ach-agent is not keeping S3 up to date
              description: The storage server in namespace ach-agent holds files S3 does not have, or has not been able to compare with S3, for over 15 minutes. Saves still succeed, but losing its volume would lose them. Check S3 reachability, the bucket credentials and the sync and reconcile containers.
      - eval_time: 46m
        alertname: ACHAgentStorageSyncLagging
        exp_alerts:
          - exp_labels:
              namespace: ach-agent
              severity: warning
            exp_annotations:
              summary: ACH agent storage in namespace ach-agent is not keeping S3 up to date
              description: The storage server in namespace ach-agent holds files S3 does not have, or has not been able to compare with S3, for over 15 minutes. Saves still succeed, but losing its volume would lose them. Check S3 reachability, the bucket credentials and the sync and reconcile containers.
          - exp_labels:
              namespace: team-b
              severity: warning
            exp_annotations:
              summary: ACH agent storage in namespace team-b is not keeping S3 up to date
              description: The storage server in namespace team-b holds files S3 does not have, or has not been able to compare with S3, for over 15 minutes. Saves still succeed, but losing its volume would lose them. Check S3 reachability, the bucket credentials and the sync and reconcile containers.
```

In `chart/tests/render_test.sh`, in the observability python block, replace

```python
assert len(alerts) == 6 and "ACHAgentStartFailing" in alerts and "ACHAgentKeyRefreshFailing" in alerts, alerts
```

with

```python
assert len(alerts) == 9 and "ACHAgentStartFailing" in alerts and "ACHAgentKeyRefreshFailing" in alerts, alerts
assert {"ACHAgentStorageDown", "ACHAgentStorageDiskHigh", "ACHAgentStorageSyncLagging"} <= set(alerts), alerts
```

and replace `pm, = of("PodMonitor")` with `pm, = of("PodMonitor")  # no storage PodMonitor without a bucket`.

After that python block (before `echo PASS`) add:

```bash
out=$(helm template t . "${base[@]}" "${on[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent,team-b}')
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
pms = {d["metadata"]["name"]: d for d in docs if d["kind"] == "PodMonitor"}
assert sorted(pms) == ["ach-agent-control", "ach-agent-storage"], sorted(pms)
spec = pms["ach-agent-storage"]["spec"]
assert spec["namespaceSelector"] == {"matchNames": ["ach-agent", "team-b"]}, spec
assert spec["selector"]["matchLabels"]["app.kubernetes.io/name"] == "ach-agent-storage", spec
assert [e["port"] for e in spec["podMetricsEndpoints"]] == ["metrics", "reconcile"], spec
print("storage observability ok")
PY
```

Run the chart checks (the command block at the top of Task 2). Expected: `EXIT=1`; render fails on the alert count (6, want 9).

- [ ] **Step 2: PodMonitor**

Append to `chart/templates/podmonitor.yaml`:

```yaml
{{- if and .Values.metrics.podMonitor.enabled (eq (include "ach-agent.storage.active" .) "true") }}
---
# Scrapes the storage server of every Agent namespace: SeaweedFS (metrics) and the reconcile gauges.
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: ach-agent-storage
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "ach-agent.labels" . | nindent 4 }}
    {{- with .Values.metrics.podMonitor.labels }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
spec:
  namespaceSelector:
    matchNames:
      {{- toYaml .Values.storage.namespaces | nindent 6 }}
  selector:
    matchLabels:
      {{- include "ach-agent.storage.selectorLabels" . | nindent 6 }}
  podMetricsEndpoints:
    - {port: metrics, path: /metrics}
    - {port: reconcile, path: /metrics}
{{- end }}
```

- [ ] **Step 3: Alerts**

Append to `chart/alerts/ach-agent.rules.yaml`:

```yaml
- name: ach-agent-storage
  interval: 30s
  rules:
    # Ready replicas come from kube-state-metrics: a pod that does not exist has no scrape target.
    - alert: ACHAgentStorageDown
      expr: |
        max by (namespace) (kube_statefulset_status_replicas_ready{statefulset="ach-agent-storage"}) < 1
      for: 5m
      labels:
        severity: critical
      annotations:
        summary: ACH agent storage server is down in namespace {{ $labels.namespace }}
        description: >-
          The storage server ach-agent-storage in namespace {{ $labels.namespace }} has had no
          ready pod for 5 minutes. Workspace and session saves and restores in that namespace
          fail until it is back.
    # The server evicts data already in S3 above storage.evictThreshold (default 0.8); usage that
    # stays above 0.85 is data S3 does not have yet.
    - alert: ACHAgentStorageDiskHigh
      expr: |
        max by (namespace, pod) (
          SeaweedFS_volumeServer_resource{type="used", pod=~"ach-agent-storage-.*"}
          / ignoring (type)
          SeaweedFS_volumeServer_resource{type="all", pod=~"ach-agent-storage-.*"}
        ) > 0.85
      for: 15m
      labels:
        severity: warning
      annotations:
        summary: ACH agent storage volume is {{ $value | humanizePercentage }} full in namespace {{ $labels.namespace }}
        description: >-
          The storage server in namespace {{ $labels.namespace }} cannot evict below
          storage.evictThreshold, so most of its volume holds data S3 does not have yet. Check S3
          reachability and the sync container; grow the volume if the backlog is legitimate.
    # The reconcile loop compares with S3 every 10 minutes: unsynced files are older than 5
    # minutes and missing from S3; the timestamp stops while S3 cannot be listed.
    - alert: ACHAgentStorageSyncLagging
      expr: |
        max by (namespace) (ach_agent_storage_unsynced_files) > 0
        or
        max by (namespace) (time() - ach_agent_storage_reconcile_ok_timestamp_seconds) > 1800
      for: 15m
      labels:
        severity: warning
      annotations:
        summary: ACH agent storage in namespace {{ $labels.namespace }} is not keeping S3 up to date
        description: >-
          The storage server in namespace {{ $labels.namespace }} holds files S3 does not have, or
          has not been able to compare with S3, for over 15 minutes. Saves still succeed, but
          losing its volume would lose them. Check S3 reachability, the bucket credentials and the
          sync and reconcile containers.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the chart checks (top of Task 2). Expected: `EXIT=0`, `storage observability ok`, `PASS`, promtool `SUCCESS`. If promtool reports an annotation mismatch on `ACHAgentStorageDiskHigh`, compare the `humanizePercentage` output it prints and copy that exact string into the test's `summary`.

- [ ] **Step 5: Docs**

`docs/install.md`, in the Observability table, replace the `metrics.podMonitor.enabled` and `metrics.prometheusRule.enabled` rows with:

```markdown
| `metrics.podMonitor.enabled` | PodMonitor `ach-agent-control`, and `ach-agent-storage` when the storage server runs | `ach-agent-control` scrapes `:http/metrics` of every Agent control pod in any namespace and sets the `agent` label from the pod label `ach-agent.ackstorm.ai/agent`. `ach-agent-storage` scrapes SeaweedFS (`:metrics`) and the reconcile gauges (`:reconcile`) in `storage.namespaces`. `labels` adds labels to both. |
| `metrics.prometheusRule.enabled` | PrometheusRule `ach-agent` | Nine alerts: six on `ach_agent_anomalies_total`, `ach_agent_runs_total` and the exchanged ACH credential (`ACHAgentKeyRefreshFailing`), and three on the storage server (`ACHAgentStorageDown`, which needs kube-state-metrics, `ACHAgentStorageDiskHigh`, `ACHAgentStorageSyncLagging`). `labels` must match the Prometheus `ruleSelector`. |
```

`CHANGELOG.md`, append to `### Added` under `## [Unreleased]`:

```markdown
- Storage server observability: with `metrics.podMonitor.enabled`, PodMonitor `ach-agent-storage`; alerts
  `ACHAgentStorageDown` (needs kube-state-metrics), `ACHAgentStorageDiskHigh` and `ACHAgentStorageSyncLagging`.
```

- [ ] **Step 6: Commit**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
/usr/bin/git add chart/templates/podmonitor.yaml chart/alerts/ach-agent.rules.yaml chart/tests/alerts.test.yaml \
  chart/tests/render_test.sh docs/install.md CHANGELOG.md
/usr/bin/git commit -F - <<'EOF'
feat(chart): scrape the storage server and alert on it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
EOF
timeout 600 make chart-test > /tmp/chart.log 2>&1; echo "EXIT=$?"; grep -E 'FAIL|ok|PASS|SUCCESS' /tmp/chart.log
```

Expected: `EXIT=0`.

---

### Task 4: e2e: storage server in front of a stand-in S3

**Files:**
- Create: `test/e2e/s3-standin.yaml`, `test/e2e/fixtures/persist-agent.yaml`
- Modify: `test/e2e/cluster.sh` (`deploy`), `test/e2e/e2e_test.go` (new subtest between `rejected-policy` and `gateway-restart`), `test/e2e/helpers_test.go` (two helpers)

**Interfaces:**
- Consumes: Task 1's `ach_agent_storage_{persist,restore}_seconds_count{domain,outcome}` on the control pod's `/metrics` (port 8080, reachable through `agent-<name>-http`); Task 2's StatefulSet `ach-agent-storage` in the Agent namespace and the values `runtime.storage.s3.*`, `storage.namespaces`, `storage.size`.
- Produces: e2e scenario `storage-persistence`; helpers `storageCount(t, agent, op, domain, outcome string) float64` and `standinRegistries(t, uid string) int`.

- [ ] **Step 1: The stand-in S3**

`test/e2e/s3-standin.yaml`:

```yaml
# Stands in for AWS S3 behind the storage server: a plain SeaweedFS S3 with one static identity.
apiVersion: v1
kind: Namespace
metadata: {name: ach-agent-e2e-s3}
---
apiVersion: v1
kind: Secret
metadata: {name: s3-standin, namespace: ach-agent-e2e-s3}
stringData:
  identities.json: '{"identities":[{"name":"storage","credentials":[{"accessKey":"e2e-s3","secretKey":"e2e-s3-secret"}],"actions":["Admin","Read","Write","List","Tagging"]}]}'
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: s3-standin, namespace: ach-agent-e2e-s3}
spec:
  replicas: 1
  selector: {matchLabels: {app: s3-standin}}
  template:
    metadata: {labels: {app: s3-standin}}
    spec:
      securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
      volumes:
        - {name: data, emptyDir: {}}
        - {name: tmp, emptyDir: {}}
        - {name: identities, secret: {secretName: s3-standin}}
      containers:
        - name: s3
          image: chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8
          command: [/usr/bin/weed]
          args: [-logtostderr=true, server, -dir=/data, -s3, -s3.config=/etc/seaweedfs/identities.json,
                 -ip=127.0.0.1, -ip.bind=0.0.0.0, -volume.max=0, -master.volumeSizeLimitMB=64]
          env: [{name: HOME, value: /tmp}]
          ports: [{name: s3, containerPort: 8333}]
          readinessProbe: {tcpSocket: {port: s3}}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tmp, mountPath: /tmp}
            - {name: identities, mountPath: /etc/seaweedfs, readOnly: true}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
---
apiVersion: v1
kind: Service
metadata: {name: s3-standin, namespace: ach-agent-e2e-s3}
spec:
  selector: {app: s3-standin}
  ports: [{name: s3, port: 8333, targetPort: s3}]
```

- [ ] **Step 2: Deploy it and the storage server from `cluster.sh`**

In `test/e2e/cluster.sh`, in `deploy()`, after `k apply -f "$HERE/stub.yaml"` add:

```bash
  k apply -f "$HERE/s3-standin.yaml"
  k -n ach-agent-e2e-s3 rollout status deploy/s3-standin --timeout=180s
  k -n ach-agent-e2e-s3 exec deploy/s3-standin -- sh -c \
    'printf "s3.bucket.list\n" | weed shell -master=127.0.0.1:9333 | grep -qw ach-e2e ||
     printf "s3.bucket.create -name ach-e2e\n" | weed shell -master=127.0.0.1:9333'
  k -n "$AGENT_NS" create secret generic e2e-s3 --from-literal=AWS_ACCESS_KEY_ID=e2e-s3 \
    --from-literal=AWS_SECRET_ACCESS_KEY=e2e-s3-secret --dry-run=client -o yaml | k apply -f -
```

and add to the `h upgrade --install ach-agent` command, before `--wait --timeout 6m`:

```bash
    --set runtime.storage.s3.bucket=ach-e2e --set runtime.storage.s3.region=us-east-1 \
    --set runtime.storage.s3.endpointUrl=http://s3-standin.ach-agent-e2e-s3.svc.cluster.local:8333 \
    --set runtime.storage.s3.credentialsSecret=e2e-s3 \
    --set "storage.namespaces={$AGENT_NS}" --set storage.size=2Gi \
```

(`--wait` also waits for the storage StatefulSet, which is Ready only after the bucket is mounted.)

- [ ] **Step 3: The persistence Agent**

`test/e2e/fixtures/persist-agent.yaml`:

```yaml
# Workspace and session persistence through the storage server. The session closes 60 s after its
# turn, the Workspace 30 s after that; the close may retry for 300 s.
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: Agent
metadata: {name: "${NAME}", namespace: ach-agent-e2e}
spec:
  profileRef: {name: e2e}
  capabilityServer: standalone
  standalone:
    model:
      name: e2e-model
      api: openai
      baseUrl: http://e2e-stub.ach-agent-e2e.svc.cluster.local:8090/v1
      headers:
        x-litellm-api-key: {secretKeyRef: {name: e2e-model, key: apiKey}}
  workspace:
    idleTimeoutSeconds: 30
    shutdownTimeoutSeconds: 300
    persistence: {enabled: true, retentionDays: 1}
    session:
      idleTimeoutSeconds: 60
      persistence: {enabled: true, retentionDays: 1}
  channels:
    - name: hook
      type: webhook
      source: generic
      routing: {sessionKey: "e2e"}
      prompt: "Reply briefly. {{ payload | json }}"
      webhook:
        auth: {type: header_token, header: X-E2E-Token, secretRef: {name: e2e-hook, key: token}}
  expose: {service: true}
```

- [ ] **Step 4: Helpers**

Append to `test/e2e/helpers_test.go`:

```go
// storageCount reads ach_agent_storage_<op>_seconds_count{domain, outcome} from an Agent's control
// pod through its -http Service; -1 when the pod cannot be reached.
func storageCount(t *testing.T, agent, op, domain, outcome string) float64 {
	t.Helper()
	out, err := stubExec(t, fmt.Sprintf(
		"import urllib.request as u;print(u.urlopen('http://agent-%s-http.%s.svc.cluster.local:8080/metrics',timeout=10).read().decode())",
		agent, agentNS))
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "ach_agent_storage_"+op+"_seconds_count{") &&
			strings.Contains(line, `domain="`+domain+`"`) && strings.Contains(line, `outcome="`+outcome+`"`) {
			var v float64
			fmt.Sscanf(line[strings.LastIndex(line, " ")+1:], "%g", &v)
			return v
		}
	}
	return 0
}

// standinLs lists one directory of the stand-in S3's filer ("" when it does not exist).
func standinLs(path string) string {
	out, err := kubectl("", "-n", "ach-agent-e2e-s3", "exec", "deploy/s3-standin", "--", "sh", "-c",
		"printf 'fs.ls "+path+"\\n' | weed shell -master=127.0.0.1:9333")
	if err != nil {
		return ""
	}
	return out
}

// standinRegistries counts the Workspace registry.json objects of one Agent in the stand-in S3.
func standinRegistries(t *testing.T, uid string) int {
	t.Helper()
	base := "/buckets/ach-e2e/ach/" + agentNS + "/" + uid
	n := 0
	for _, ws := range strings.Fields(standinLs(base)) {
		for _, name := range strings.Fields(standinLs(base + "/" + ws)) {
			if name == "registry.json" {
				n++
			}
		}
	}
	return n
}
```

- [ ] **Step 5: The scenario**

In `test/e2e/e2e_test.go`, insert between the end of `t.Run("rejected-policy", ...)` and `t.Run("gateway-restart", ...)`:

```go
	t.Run("storage-persistence", func(t *testing.T) {
		const name = "persist"
		clearStub(t)
		apply(t, "fixtures/persist-agent.yaml", map[string]string{"NAME": name})
		t.Cleanup(func() { kubectl("", "-n", agentNS, "delete", "agent", name, "--wait=false") })
		waitCond(t, name, "Ready", "True", 5*time.Minute)
		postAccepted(t, name, 2*time.Minute)
		waitChat(t, 8090, 4*time.Minute)
		uid := agentUID(t, name)

		// The session's idle close (60 s after its turn) starts while the storage server is down.
		mustKubectl(t, "", "-n", agentNS, "scale", "statefulset/ach-agent-storage", "--replicas=0")
		until(t, 2*time.Minute, "the storage pod did not terminate", nil, func() bool {
			_, err := kubectl("", "-n", agentNS, "get", "pod", "ach-agent-storage-0")
			return err != nil
		})
		time.Sleep(90 * time.Second)
		if n := storageCount(t, name, "persist", "session", "ok"); n > 0 {
			t.Fatalf("a session save committed while the storage server was down (count %v)", n)
		}
		mustKubectl(t, "", "-n", agentNS, "scale", "statefulset/ach-agent-storage", "--replicas=1")
		mustKubectl(t, "", "-n", agentNS, "rollout", "status", "statefulset/ach-agent-storage", "--timeout=300s")

		// The retried close commits the session, then the Workspace closes and its sandbox goes.
		until(t, 10*time.Minute, "the session and workspace saves never committed", agentYAML(name), func() bool {
			return storageCount(t, name, "persist", "session", "ok") >= 1 &&
				storageCount(t, name, "persist", "workspace", "ok") >= 1
		})
		until(t, 3*time.Minute, "the stand-in S3 never received the workspace registry", nil, func() bool {
			return standinRegistries(t, uid) >= 1
		})
		until(t, 3*time.Minute, "the sandbox stayed after the workspace close", agentYAML(name), func() bool {
			return len(sandboxes(t, uid)) == 0
		})

		// A new message restores the Workspace through the storage server.
		clearStub(t)
		postAccepted(t, name, 2*time.Minute)
		waitChat(t, 8090, 4*time.Minute)
		until(t, 2*time.Minute, "the workspace restore never succeeded", agentYAML(name), func() bool {
			return storageCount(t, name, "restore", "workspace", "ok") >= 1
		})
	})

```

- [ ] **Step 6: Compile check**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs/test/e2e
timeout 300 go vet -tags e2e ./... > /tmp/vet.log 2>&1; echo "EXIT=$?"; cat /tmp/vet.log
```

Expected: `EXIT=0`.

- [ ] **Step 7: Full e2e (the one heavy gate)**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
LOG=/tmp/e2e-storage.log
timeout 5400 make e2e > "$LOG" 2>&1; echo "EXIT=$?"
grep -E '^(=== RUN|--- (PASS|FAIL)|PASS|FAIL|ok )' "$LOG"
```

Expected: `EXIT=0`; `--- PASS: TestE2E/storage-persistence` and every existing subtest passes (their profile keeps persistence off, so their storage path is unchanged). On failure, check `kubectl --context kind-ach-agent-e2e -n ach-agent-e2e logs ach-agent-storage-0 -c reconcile` (and `-c sync`, `-c server`) and the control pod log `agent-persist-0` before changing code. Leave the cluster up for inspection; `make e2e-down` removes it.

- [ ] **Step 8: Commit**

```bash
cd /home/coder/workspace/local/ach-agent/.worktrees/specs
/usr/bin/git add test/e2e/s3-standin.yaml test/e2e/cluster.sh test/e2e/fixtures/persist-agent.yaml \
  test/e2e/e2e_test.go test/e2e/helpers_test.go
/usr/bin/git commit -F - <<'EOF'
test(e2e): persist through the storage server across a storage restart

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
EOF
```

---

## Rollout (not part of this plan)

1. Terraform (user-owned): a Pod Identity association `ach-agent/ach-agent-storage` on the existing bucket role; remove the one on `ach-agent/ach-agent-control` after step 3.
2. Release 0.3.0: `make release-bump VERSION=0.3.0`, edit CHANGELOG, `chore(release): v0.3.0` on `main`.
3. GitOps: bump the chart with `storage.namespaces: [ach-agent]` and the bucket's region; the operator rolls control pods onto the storage endpoint.
