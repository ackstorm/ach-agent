# Plan 1 report

Branch `feat/bootstrap` in `/home/coder/workspace/local/ach-agent` (not pushed). Imported ach-runtime `ebc3409e2c2be6164a023136f272de143239c39c`.

## Commits

| sha | subject |
|---|---|
| a8e381d | chore: retire the legacy runtime tree |
| c367eb8 | chore: import ach-runtime ebc3409e… into runtime/ |
| b05f022 | chore: set up the ach-agent repo around the imported runtime |
| 99b5075 | feat(config): workspace-v3 capability block (standalone or ach) |
| 88ae4f0 | feat(harness): standalone catalog and per-server assembly |
| 6015289 | feat(facades): per-upstream headers replace inbound ones case-insensitively |
| aea7e20 | feat(facades): model facade enforces configured model and path allow-list |
| 1a3c159 | feat(observability): metrics and redaction in standalone, unpriced cost |
| b1838a8 | feat(harness): defer recovery and retry unsent starts while the gateway is down |
| 295660c | test: give channel app stubs agent identity; align contract docs to workspace-v3 |
| 014a289 | chore: set the runtime version to 0.19.0 |
| 1fcbbc8 | test: vendor the fake-facades fixture and restore the deleted e2e and engine tests |
| 6a53a71 | feat(harness): raise the gateway_unavailable alarm when a start cannot reach the workspace |

## Gate results

After the follow-ups (S3 compose fixture up, then torn down):

- Unit suite excluding e2e, in the devtools image: 1474 passed, `EXIT=0`. A full `make test-unit` run under heavy host load (load average above 20) hit the 600 s foreground cap, so the suite was split.
- `tests/e2e`, same image: 31 passed, `EXIT=0` (360 s).
- Combined: 1505 passed, 0 failed. The earlier single run of the full suite before item 3 passed 1504, `EXIT=0`.
- `make compose-e2e` (the CI single-unit acceptance): `EXIT=0`, `PASS: restored after recreate`.
- `make schema-check`: `EXIT=0`.
- `grep -rn "config\.ach\b\|workspace-v2" src tests examples docs/schemas scripts`: one intentional hit, `src/ach_runtime/config/schema.py` (the check that rejects `workspace-v2` configs).

## Deviations from the plan

- `origin/main` of `ackstorm/ach-agent` already carried a legacy `v0.18.0` release (tags `v0.17.0` and `v0.18.0`), so the runtime version is `0.19.0` (follow-up item 1).
- `docs/references` does not exist at the source SHA and was not imported.
- `src/ach_runtime/__init__.py` also read the old distribution name; changed to `ach-agent-runtime`.
- `Dockerfile.devtools`: dropped the `spikes/` COPY and the spike-probe stage; the vendored fixture arrives through the `tests/` copy.
- The tests that need the fake-facades fixture were deleted in the first pass and restored in the follow-ups: the fixture is vendored as `runtime/tests/fixtures/fake-facades.mjs` (a copy of that single file from ach-runtime `spikes/`), and the deleted tests come unchanged from ach-runtime main `ebc3409e`, except `tests/e2e/test_pr_review.py`, which reads `config.identity_environment`.
- `test-unit` forwards the `ACH_STORAGE_TEST_S3_*` variables; the gate needs the SeaweedFS compose fixture (`docker network create ach-runtime-seaweed-e2e_test` then `docker compose --profile fixture up -d --wait s3`), as CI does. It was torn down afterwards.
- `runtime.yml` keeps the temp-dir build context instead of `context: runtime`.
- `Control` is a slots dataclass: `_priced` is a dataclass field plus a `getattr(..., True)` read, not a class attribute.
- Task 2 left `tests/harness/test_hydrate.py` red until Task 3 (stub config); noted in the commit body.
- Channel test stubs (`tests/channels`) needed `agent` and `identity_environment` once `/metrics` is always mounted.
- Contract docs: `workspace-v2` mentions in the D1 row and the migration note were rewritten to `workspace-v3`.
- The chart-less `make gen-schema` uses docker; the schema was regenerated with the venv script and `make schema-check` confirms it matches.

## Open issues

- `.claude/repo_cost.json` appeared untracked in the new checkout and was left alone.
- The legacy checkout is at `/home/coder/workspace/local/ach-agent-legacy`; `git worktree repair` printed "`.git` file broken" for three sibling worktrees (`ach-agent-cost-source`, `-hydrated-info`, `-metrics-design`) but their `.git` files now point at the legacy path and `git worktree list` shows them.
- A full `make test-unit` takes about 6 minutes on an idle host and can exceed a 600 s foreground cap under load.

## Review fixes

Findings from `plan1-review.md`, each checked against the code first. Commits:

| sha | subject |
|---|---|
| b20f620 | fix(facades): model facade rejects duplicate keys, routing fields and stacked content encodings |
| 495019c | fix(facades): mcp facade checks every Content-Encoding value before inspecting a tool call |
| 70fbb26 | test(facades): use a header the drop list does not cover in the injected-header test |
| 99c2602 | fix(delivery): wait for the gateway before sending, retry refused connects only while it is down |
| b62a357 | fix(workspaces): run a deferred adoption as the ref's adopt task |
| 130379f | fix(config): standalone model baseUrl and MCP url must be http(s) URLs with a host |
| ffa57f3 | fix(observability): redact the token behind an auth scheme and the memory bearer secret |

| # | Result |
|---|---|
| 1 | Confirmed (`json.loads` keeps the last duplicate). Duplicate keys at any depth now answer 400 `duplicate key`; test added. |
| 2 | Confirmed. For openai and anthropic, top-level `fallbacks`, `user_config`, `api_base`, `base_url`, `api_key` answer 400; ten parametrized cases. |
| 3 | Confirmed. A refused connect is retried only while `gateway_available()` is false; otherwise it takes the same-instance replay path (`StartOutcomeUnknown`). |
| 4 | Confirmed. Before the endpoint is resolved, a Start waits on `WorkspaceManager.wait_gateway(deadline)` while the host reports the gateway down; still down at the deadline raises `GatewayUnavailable`. Nothing is sent before it returns. This is earlier than the review suggested (before the endpoint is resolved and the request prepared), so no stale endpoint or prepared context is reused. |
| 5 | Already fixed by 6a53a71 (`GatewayUnavailable`, `Control.run` alarms `gateway_unavailable`, test in `test_control.py`). Verified, skipped. |
| 6 | Confirmed. `FakeSource` now has an event-backed `wait_gateway` and a `gateway` property; the deferral test flips the flag and relies on the scheduled task. It does not fail without the production change (the old task also works once the fake can signal); the new ensure/cancel tests do. |
| 7 | Confirmed. The test now configures `X-Custom-Token`, sends `x-custom-token: guess` and asserts one value; with the replacement rule removed it fails (`2 == 1`). |
| 8 | Confirmed for Starlette (`Headers.get` returns the first value). New `has_encoded_body` checks every repeated and comma-listed value. The extra suggestion to drop `content-encoding` from forwarded request headers is not needed: when every value is `identity` nothing encoded is forwarded, and dropping it in `forward()` would change the memory and A2A facades too. |
| 9 | Confirmed. The MCP facade uses `has_encoded_body`; a gzip body behind `identity, gzip` headers answers 415 with no upstream call. |
| 10 | Confirmed. Module docstring rewritten. |
| 11 | Confirmed. The deferred task is registered as the ref's `"adopt"` task (replacing the finishing attempt, under the ref's lock) and `cancel_adoption()` cancels it. |
| 12 | Confirmed. `ensure()` now also awaits a pending `"adopt"` task, so it no longer reaches `place()` against a still-hosted unit; test added. |
| 13 | Confirmed. `baseUrl` and `url` use the endpoint pattern (also in the generated JSON schema); four bad values tested for both fields. |
| 14 | Confirmed. `register_secrets` also registers the token behind a leading scheme, and `assemble` registers the memory header values; tests for both. |
| 15 | Confirmed. The request uses `httpx.Timeout(timeout, connect=min(timeout, 10))`; a test reads the connect timeout from the request extensions. |

Gates (S3 compose fixture up, then torn down, each run with `timeout 900`): unit suite excluding e2e 1500 passed (`EXIT=0`, 132 s); `tests/e2e` 31 passed (`EXIT=0`, 265 s); `gen_schema.py --check` `EXIT=0`. Total 1531 passed.

## Code-review fixes

Findings from `/code-review` on `feat/bootstrap` @ 8d7d330, all verified against the code and accepted.

| Commit | Findings |
|---|---|
| cdf19c4 | 1: extra egress is external only. A non-reserved endpoint with a dotless host, a `.svc` / `.cluster.local` host, or an IP host or `allowed_ips` range overlapping loopback, link-local, RFC1918, CGNAT, ULA or IPv6 link-local is rejected (`ErrPolicyRejected`). Spec and `operator/CLAUDE.md` updated. |
| 93ff061 | 4: a refusal is cleared once a complete pass has no refused running Workspace (also after the refused Workspace is released); the message records the AgentProfile generation and `sandboxRefusalStands` takes it into account. 7: the refusal is written once per (reason, message, generation). 8: the refusal reasons and key Secret name/data key are defined once in `host` and used by the controller and openshell; the pin test is removed. 5: `coordination.k8s.io` leases RBAC (events already present), `make gen`, fail-fast when leader election has no namespace. |
| cbd4848 | 2: gemini bodies are parsed (non-JSON, duplicate keys, non-object, routing fields rejected). 6: gemini model ids may contain `/`; empty, `.` and `..` segments are rejected and the id must equal the configured model. |
| b0e3fe6 | 3: standalone memory via `mcpServerId` carries that server's resolved headers, the memory `auth` header wins on the same name (case-insensitive). |
| f96a197 | 9: `wait_gateway` is declared on `WorkspaceSource`; `LocalSource` implements it; the `getattr` probes and the sleep fallback are gone. |
| d8aa29c | 10: header entries are `{"env": NAME}` or `{"value": "..."}`. Only env-derived values are registered for redaction; the operator renders literals as `{"value": ...}` without an env binding. Schema regenerated and re-vendored byte-equal. |

Each fix has a test that fails without it, except 5's RBAC marker (generated `role.yaml` is covered by `make verify` drift check) and 9 (a refactor; the existing delivery and lifecycle tests cover it).

Gates (S3 compose fixture up, then torn down; the kind cluster was not touched):

- runtime unit suite excluding e2e: 1522 passed (`EXIT=0`, 159 s); `tests/e2e`: 31 passed (`EXIT=0`, 244 s); `gen_schema.py --check`: `EXIT=0`.
- operator `make verify`: `EXIT=0`; `make test-envtest`: `EXIT=0`.
