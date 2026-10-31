# Storage server — design

Status: draft, 2026-10-31. Conditional on the SeaweedFS spike (section 9).
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
- **Init step** (idempotent): `remote.configure` the AWS bucket, `remote.mount` it at the
  bucket path the runtime uses, with lazy metadata.
- **`filer.remote.sync`** runs as a second container, writing local changes back to S3. It
  resumes from its saved offset after a restart.
- **Evictor**: a small loop container. Above a high watermark (default 80 % of the volume) it
  runs `remote.uncache` on the least recently used paths until the low watermark (70 %),
  never on a path whose latest change is not yet synced.
- **Service** `ach-agent-storage:8333`, reachable only from the namespace's control pods
  (Istio AuthorizationPolicy on the control ServiceAccount; sandboxes are denied).
- **Credentials**
  - Storage server → S3: Pod Identity on ServiceAccount `ach-agent-storage`. One association
    per namespace in Terraform; it replaces the association on `ach-agent-control`.
  - Control pod → storage server: a static S3 access key the chart generates into a Secret.
    The operator already wires `runtime.storage.s3.credentialsSecret` as
    `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on control pods
    (`operator/internal/render/storageconfig.go`).

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
  `storage.s3.{bucket,region,prefix}`.

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
| S3 unreachable | Saves keep being acknowledged; the backlog grows on the volume. | Alert `ACHAgentStorageSyncLagging` on sync lag age and backlog size. |
| Volume lost | Acknowledged changes not yet in S3 are lost. | Bounded by the sync lag; the same alert. EBS volume loss is rare. |
| Volume full | Writes fail once eviction cannot free space (all unsynced). | Alert at the high watermark; size the volume for the backlog of an S3 outage. |
| Zone failure | The RWO EBS volume is pinned to one zone; the pod waits for it. | Accepted for v1; same exposure as today's single control pod per agent. |

## 8. Observability

- Runtime histograms (section 5).
- SeaweedFS's own Prometheus metrics, scraped by a ServiceMonitor.
- Sync lag and backlog: from `filer.remote.sync` if it exposes them (spike check 6), else a
  probe in the evictor that compares the newest local change with the newest synced offset.
- Alerts: `ACHAgentStorageSyncLagging`, `ACHAgentStorageDiskHigh`, `ACHAgentStorageDown`,
  each with a promtool test in `make chart-test`.

## 9. Spike (gate for this design)

Run in kind with the SeaweedFS image to be pinned, a second S3 endpoint standing in for AWS,
and the runtime's own storage tests pointed at the storage server. Each check passes or the
design falls back to section 10.

1. **Conditional writes through the mounted bucket**: `IfNoneMatch="*"` returns 412 on an
   existing key, for a small object and for a multipart upload (> 8 MiB); `IfMatch` CAS on
   `registry.json` succeeds with the current ETag and fails with a stale one, also after a
   storage pod restart. The runtime's existing `tests/storage` suite passes against it.
2. **Credentials**: the S3 replica is reached with the default AWS chain (Pod Identity, or
   IRSA via `AWS_WEB_IDENTITY_TOKEN_FILE`), with no static AWS keys.
3. **Eviction safety**: `remote.uncache` never drops a path with unsynced changes; with S3
   unreachable, eviction frees nothing and writes still succeed until the disk is full.
4. **Restart durability**: `kill -9` of both containers while an upload is pending; after
   restart the object reaches S3 unchanged.
5. **Layout**: keys written through the storage server appear in S3 under exactly the same
   names; objects written to S3 before the switch are readable through it.
6. **Sync lag**: a measurable signal for backlog age and size exists or can be derived.
7. **Speed**: persist and restore of a 50 MiB and a 300 MiB snapshot, before and after.

## 10. Fallback: our own narrow service

Only if the spike fails. A Python service in the runtime image implementing the existing
`Storage` protocol over HTTP:

- Snapshots are immutable, so write-back is a queue of local files not yet uploaded; the
  uploader reuses `S3Storage`, which already handles credentials and conditional writes.
- Single writer becomes an in-process lock per state ref: one server, one owner.
- LRU over the volume, skipping files still in the upload queue.
- The Harness swaps `S3Storage` for an HTTP client of the same protocol.

## 11. Testing

- Spike results recorded in this spec's follow-up plan.
- e2e (kind): storage server in front of a stand-in S3; the existing sleep/wake and
  persistence scenarios pass unchanged; a storage pod restart during an idle close is
  retried and commits.
- `make chart-test`: render checks for the StatefulSet, Service, AuthorizationPolicy and
  alerts.

## 12. Rollout

1. Terraform: a Pod Identity association for `ach-agent/ach-agent-storage` on the existing
   bucket policy; remove the one on `ach-agent-control` after the switch.
2. ach-agent 0.3.0 with `storage.enabled: true`.
3. GitOps: bump the chart; the operator rolls control pods onto the storage endpoint.
   Agents with a session in flight finish it on the old path; new saves go through the
   storage server.
