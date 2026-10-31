# Storage server — design

Status: draft, 2026-10-31. SeaweedFS spike passed with caveats (section 9): SeaweedFS 4.51 or newer.
Next spec: `2026-10-31-durable-runs-design.md`, which depends on this one.

## 1. Goal

One storage server per Agent namespace holds workspace and session snapshots on a local
volume and replicates them to S3 in the background.

- **Faster saves and restores.** A save finishes when the snapshot is on the storage server's
  disk; the S3 upload leaves the critical path. A restore of recently used state reads the
  local copy.
- **One S3 identity per namespace.** Only the storage server holds AWS credentials. Control
  pods and sandboxes hold none.
- **A save fast enough for durable runs**, which must store a workspace inside a pod's
  termination grace period.

Non-goals: sharing state across Harness replicas, multi-replica storage, artifacts (they keep
their current path until a later change moves them), changing the snapshot format.

## 2. Decisions

| Decision | Choice |
|---|---|
| Topology | One storage server per Agent namespace, installed by the chart. |
| Source of truth | The storage server. A save is done when it acknowledges it, before any S3 upload. Keeping it durable in S3 is the storage server's job. |
| S3 role | Asynchronous replica and the source for cache misses. |
| Remote bucket | Production always replicates to a real bucket: AWS S3 (today's bucket, same keys, existing objects readable) or Google Cloud Storage (section 4.1). Every stand-in S3 or GCS server is test-only. |
| Local disk | A bounded cache with least-recently-used eviction. Only data already in S3 can be evicted. |
| Software | SeaweedFS remote storage, if the spike passes (section 9). Otherwise a narrow service of our own (section 10). |
| Contract | Reverses decision D1 ("Harness owns the S3 client, no Storage broker", `runtime/docs/schemas/ach-workspace-contract-v1.md:246-263`). The runtime still speaks S3; the storage server is an S3 endpoint, not a new protocol. |

## 3. Current path (for reference)

- `S3Storage` (`runtime/src/ach_runtime/storage/s3.py`) is the only S3 client: sync boto3
  under `asyncio.to_thread`, ambient credentials on the control pod.
- A save streams the archive out of the unit to a temp file, stages it again in `persist`,
  then sends one `put_object` (512 MiB cap). A restore downloads to disk, verifies the
  checksum and streams it into the unit.
- Single writer: `registry.json` per state ref, written with `IfNoneMatch="*"` / `IfMatch`
  conditional PUTs; snapshots and commit records are create-only.
- Keys: `{prefix}/{namespace}/{agent_uid}/{workspace_ref}[/sessions/{session_ref}]/
  {registry.json, snapshots/<id>.tar.gz, commits/<id>.json}`.
- No metrics for save or restore duration.

## 4. Architecture

```
control pod ──S3 API (path-style, static key)──▶ ach-agent-storage (StatefulSet, 1 replica)
                                                   ├─ weed server -s3 (master, volume, filer, S3)
                                                   ├─ weed filer.remote.sync  ──▶ AWS S3 bucket
                                                   └─ evictor (disk watermark loop)
                                                   PVC: EBS gp3
```

- **StatefulSet `ach-agent-storage`**, one replica on one RWO volume, so two pods never share
  the cache. No PodDisruptionBudget that blocks drains: a drain moves it, and saves retry
  through the short outage (section 7).
- **Init step** (idempotent): `remote.configure -type=wasabi` with the AWS regional endpoint
  and no keys (the `s3` type signs anonymously without static keys; `wasabi` uses the default
  AWS chain, so Pod Identity), then `remote.mount -nonempty` of the bucket.
- **`filer.remote.sync`** runs as a second container, writing local changes back to S3. It
  resumes from its saved offset after a graceful restart.
- **Reconcile**: on every start, and every 10 minutes, `remote.copy.local` uploads any local
  change the sync stream missed. A `kill -9` loses the change events of the last 1–2 minutes
  (the data stays on the volume); the reconcile uploads it.
- **Evictor**: a small loop container. Above a high watermark (default 80 % of the volume) it
  runs `remote.uncache` on the oldest paths first until the low watermark (70 %).
  `remote.uncache` skips paths with unsynced changes (verified), and has no LRU or
  target-size option of its own, so the loop picks the order.
- **Service** `ach-agent-storage:8333`, reachable only from the namespace's control pods
  (Istio AuthorizationPolicy on the control ServiceAccount; sandboxes are denied).
- **Credentials**
  - Storage server → S3: Pod Identity on ServiceAccount `ach-agent-storage`. One association
    per namespace in Terraform; it replaces the association on `ach-agent-control`.
  - Control pod → storage server: a static S3 access key the chart generates into a Secret.
    The operator already wires `runtime.storage.s3.credentialsSecret` as
    `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on control pods
    (`operator/internal/render/storageconfig.go`).

### 4.1 Remote bucket: S3 or GCS

`storage.remote.type` picks the bucket the storage server replicates to (default `s3`). The
runtime speaks S3 to SeaweedFS in both cases, so GCS needs no runtime change; it does need
`storage.enabled`, because control pods reach GCS only through the storage server.

| | `s3` | `gcs` |
|---|---|---|
| Bucket, prefix | `runtime.storage.s3.{bucket,region,prefix}` (and `endpointUrl` for an S3-compatible service) | `storage.remote.gcs.{bucket,project,prefix}` |
| SeaweedFS remote | `remote.configure -type=wasabi` (default AWS chain) | `remote.configure -type=gcs -gcs.projectId=<project>` (Application Default Credentials) |
| Credentials | Pod Identity on ServiceAccount `ach-agent-storage`, or `runtime.storage.s3.credentialsSecret` | GKE Workload Identity through an annotation on ServiceAccount `ach-agent-storage` (`storage.serviceAccount.annotations`), or an existing key Secret `storage.remote.gcs.credentialsSecret` (`GOOGLE_APPLICATION_CREDENTIALS`) |

With `gcs` the SeaweedFS bucket carries the GCS bucket's name, so the operator gives control
pods `storage.remote.gcs.bucket` and `prefix` as their S3 bucket and prefix. Verified on
2026-10-31 against `fsouza/fake-gcs-server` through the Go SDK's `STORAGE_EMULATOR_HOST`:
mount, sync of objects written through S3, create-only 412, eviction, read-through and
objects present before the mount. Real Workload Identity is not verified.

## 5. Runtime and operator changes

Small on purpose: the runtime keeps its S3 client.

- `ACH_STORAGE_S3_ENDPOINT_URL` points at the storage Service (chart value, defaulted when
  the storage server is enabled).
- Path-style addressing when an endpoint URL is set (`botocore` `Config(s3={"addressing_style":
  "path"})` in `storage/config.py`), since `<bucket>.<service>` does not resolve in-cluster.
- Metrics: `ach_agent_storage_persist_seconds` and `ach_agent_storage_restore_seconds`
  histograms, labelled `domain` (`workspace` | `session`) and `outcome`.
- Chart: `storage.enabled` (default `true`), `storage.size`, `storage.storageClass`,
  `storage.watermarks.{high,low}`, `storage.image` (pinned SeaweedFS version),
  `storage.s3.{bucket,region,prefix}`, `storage.remote.type` (`s3` | `gcs`),
  `storage.remote.gcs.{bucket,project,prefix,credentialsSecret}`,
  `storage.serviceAccount.annotations`.

## 6. Semantics

- **Write**: a `PutObject` acknowledged by the storage server is durable on its volume. The
  runtime treats it exactly as it treats an S3 acknowledgement today.
- **Conditional writes**: evaluated by the storage server against its own state, which is
  authoritative. Fencing keeps working because every writer goes through the one server.
- **Read**: served from the volume; on a miss, fetched from S3, cached, then served.
- **Eviction**: only the local copy is dropped; the object stays readable through
  read-through.
- **Existing data**: objects already in the bucket under the current layout are readable on
  first access (lazy metadata), so no migration step is needed.

## 7. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Storage pod restarting or moving | Saves and restores fail for its duration. | Existing close retries (backoff to 30 s within `shutdownTimeoutSeconds`); a failed restore fails activation and the next message retries it. |
| S3 unreachable | Saves keep being acknowledged, but each write of a new key waits about 30 s on the remote lookup (measured with a refused connection; a blackholed endpoint may be slower). The backlog grows on the volume. | Close retries absorb it; durable-run saves at SIGTERM may not fit the grace period during an outage. Alert `ACHAgentStorageSyncLagging`. |
| Storage pod killed (`kill -9`) | The last 1–2 minutes of changes stay local but miss the sync stream. | The start-up reconcile uploads them. |
| Volume lost | Acknowledged changes not yet in S3 are lost. | Bounded by the sync lag; the same alert. EBS volume loss is rare. |
| Volume full | Writes fail once eviction cannot free space (all unsynced). | Alert at the high watermark; size the volume for the backlog of an S3 outage. |
| Zone failure | The RWO EBS volume is pinned to one zone; the pod waits for it. | Accepted for v1; same exposure as today's single control pod per agent. |

## 8. Observability

- Runtime histograms (section 5).
- SeaweedFS's own Prometheus metrics, scraped by a ServiceMonitor.
- Sync lag: `SeaweedFS_filer_last_send_timestamp_of_subscribe{clientName="filer.remote.sync"}`
  on the filer's metrics port. Backlog size: the evictor loop exports the count of
  `remote.copy.local -dryRun` entries.
- Alerts: `ACHAgentStorageSyncLagging`, `ACHAgentStorageDiskHigh`, `ACHAgentStorageDown`,
  each with a promtool test in `make chart-test`.

## 9. Spike results (2026-10-31)

Docker, SeaweedFS 4.51 (`chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8`),
a second SeaweedFS standing in for AWS. 4.48 fails check 3: objects created through the
storage server are never marked synced, so they can never be evicted.

| # | Check | Result |
|---|---|---|
| 1 | Conditional writes (small, 20 MiB, multipart), CAS after restart, runtime `tests/storage` (222) | Pass |
| 2 | Default AWS chain, no static keys | Pass with `-type=wasabi` (Pod Identity endpoint emulated); `-type=s3` goes anonymous |
| 3 | Eviction never drops unsynced data; writes succeed with S3 down | Pass, but ~30 s per new-key write while S3 is down |
| 4 | `kill -9` with uploads pending | Data kept; last 1–2 min of changes need `remote.copy.local` (in the design) |
| 5 | Same key layout; existing objects readable; read-through after eviction | Pass; objects written to S3 directly after the mount need a metadata sync (not our case) |
| 6 | Sync lag signal | Partial: last-send timestamp metric; backlog from `remote.copy.local -dryRun` |
| 7 | Speed (local, relative) | Put 50 MiB on par, get on par; read-through after eviction 0.2 s (50 MiB) |

Other findings: a remote endpoint resolving to a private IP (an S3 interface VPC endpoint) needs
the three `*.allowUntrustedRemoteEndpoints` flags; `remote.copy.local` also uploads leftover
parts of failed multipart uploads (an S3 lifecycle rule aborting incomplete uploads covers it).

## 10. Fallback: our own narrow service

Kept for reference; the spike passed. A Python service in the runtime image implementing the existing
`Storage` protocol over HTTP:

- Snapshots are immutable, so write-back is a queue of local files not yet uploaded; the
  uploader reuses `S3Storage`, which already handles credentials and conditional writes.
- Single writer becomes an in-process lock per state ref: one server, one owner.
- LRU over the volume, skipping files still in the upload queue.
- The Harness swaps `S3Storage` for an HTTP client of the same protocol.

## 11. Testing

- e2e (kind): storage server in front of a stand-in S3; the existing sleep/wake and
  persistence scenarios pass unchanged; a storage pod restart during an idle close is
  retried and commits.
- `make chart-test`: render checks for the StatefulSet, Service, AuthorizationPolicy and
  alerts, for both remote types.
- Docker smoke (`make storage-smoke`): the storage server in front of a stand-in SeaweedFS S3
  and in front of `fake-gcs-server`. The kind e2e runs the S3 variant only.

## 12. Rollout

1. Terraform: a Pod Identity association for `ach-agent/ach-agent-storage` on the existing
   bucket policy; remove the one on `ach-agent-control` after the switch.
2. ach-agent 0.3.0 with `storage.enabled: true`.
3. GitOps: bump the chart; the operator rolls control pods onto the storage endpoint.
   Agents with a session in flight finish it on the old path; new saves go through the
   storage server.

For a GCS bucket instead, step 1 is the GCS bucket and a Workload Identity binding for
`<namespace>/ach-agent-storage` (user-owned infrastructure), and the chart sets
`storage.remote.type: gcs`.
