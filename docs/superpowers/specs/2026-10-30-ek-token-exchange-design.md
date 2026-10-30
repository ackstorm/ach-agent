# Environment credentials by ServiceAccount token exchange

Status: design approved in conversation, 2026-10-30; revised after two reviews (the ACH session and
Codex). Owner: ach-agent project. Touches two repos: ACH (`ackstorm/ach`, the provider of the
contract) and ach-agent (`ackstorm/ach-agent`, its client).

## 1. Goal

An agent in ACH mode needs an Environment credential for one identity and one Environment. Today a
person mints an `ek_` by hand, stores it in Secrets Manager, and an ExternalSecret copies it into the
agent's namespace (`workloads/agents/achagents/k8s-doctor.yaml` in GitOps). That leaves a long-lived
key at rest in three places and a manual step per agent.

With this design the agent's control pod proves who it is with its Kubernetes ServiceAccount token.
ACH answers with a short-lived ACH JWT that stands for one stable `ek_` held inside ACH. The raw
`ek_` never leaves ACH. The pod asks for a new JWT before the current one expires and swaps it with
no failed request.

## 2. Requirements

- **R1. Opt-in per identity.** A credential is issued only for an identity a platform admin named
  explicitly in a grant. A human user's identity (any employee, the CEO) is never usable unless
  someone writes a grant naming that exact identity. No wildcards or patterns anywhere in a grant.
- **R2. Grant checks plus today's check.** Every exchange verifies that one grant names the
  identity, the Environment and the requesting ServiceAccount. The existing rule still applies: the
  identity's teams intersect the Environment's `authorizedTeams`.
- **R3. No key at rest outside ACH.** No `ek_` and no JWT is written to etcd, Git, Secrets Manager, a
  log, an audit record or a metric.
- **R4. Transparent rotation.** A 24x7 agent replaces its JWT with zero failed requests, including
  requests in flight, long streams and MCP sessions.
- **R5. Auditable.** Every exchange, granted or denied, emits an audit event without credential
  values.
- **R6. The Secret path stays.** An Agent can still read an `ek_` from a Secret
  (`keyFrom.secretKeyRef`), for agents outside ACH's cluster or for manual keys.

Identities are the existing ACH identities (emails). `ai.run@ackstorm.com` is a real Google
Workspace account with its own Drive, Calendar, Zoho and GitLab access; it is the agents' identity.
No new service-identity type is introduced.

## 3. Facts this design relies on

ACH (v1.0.1):

- `POST /platform/keys` creates an `ek_` only for a `pk_` caller
  (`internal/platformapi/envkeys/handler.go:195` `CreateHandler`). Order today: quota
  (`handler.go:250`), Environment load (503 `not_ready` when unsynced), team check
  (`validateTeamMembership`, `handler.go:398`, 403 `unauthorized_team`), then `mintAndInsert`
  (`handler.go:476`). `mintAndInsert` writes the HTTP response and emits `platform.ek.create`
  itself (`handler.go:665-679`).
- An `ek_` is stored only as a peppered hash; the backing LiteLLM key is stored encrypted
  (`litellm_key_material_enc`). The raw `ek_` is returned once and is not recoverable.
- Key metadata `ach_issuer` carries the ACH release's base URL; the orphan reaper uses it to tell
  ACH-owned LiteLLM keys from foreign ones (`handler.go:554`, `internal/orphan/runnable.go:257`).
- Quota: `UserKeyAllowance` (`internal/db/user_limits.go:28-45`) counts the owner's non-revoked
  Environment keys, expired ones included, against `max_keys`. The check is not atomic with the
  insert.
- Environments are CRDs projected into Postgres by the operator; the platform API reads the
  projection. All ACH CRDs are namespaced; the operator watches one namespace (`ACH_NAMESPACE`,
  code default `ach-system`, production `ach`).
- Credentials are resolved by `keystore.Resolver` in the forwarder, content-service and platform API,
  behind a Redis cache with a 60-second window (`internal/keystore/keystore.go:200`). The forwarder's
  `Authn` runs before `EkOwnerGate`, which checks owner access, not key validity
  (`internal/forwarder/server.go:95`, `proxy/gate.go:25`). LiteLLM then authenticates the backing key
  on each request (`proxy/proxy.go:134`).
- ACH already maps an ACH JWT to a key row: `keystore.NewOAuthResolver`
  (`internal/keystore/oauthresolver.go`) verifies a compact JWS with the shared Ed25519 signer
  (`internal/forwarder/jwt/verify.go`, current and next key slots) and resolves its subject to the
  user's OAuth `pk_` row, inside the Redis cache. It is wired with audience `ach` in the forwarder,
  content-service and platform API (`cmd/ach/cmd/{forwarder,content_service,platform_api}.go`).

ach-agent (v0.1.5):

- `Agent.spec.ach` has `baseUrl` (HTTP allowed today), optional `environment`, required
  `keySecretRef`, `model` (`operator/api/v1alpha1/agent_types.go`, `AchServerSpec`).
- The operator passes the key as env `ACH_SECRET_IDENTITY` (`operator/internal/render/workspacev1.go:45`,
  `wsconfig.go:297`) and hashes referenced Secrets into the pod config hash
  (`operator/internal/controller/agent_controller.go:327`).
- The control pod's ServiceAccount is `ach-agent-control-<agent uid>` unless the profile sets one
  (`operator/internal/render/wsconfig.go:126`).
- The harness reads the key once at assembly. It is copied into: the model and MCP facade header
  dicts (`runtime/src/ach_runtime/harness/control.py:887-900`); memory auth headers (`:927`) and the
  memory MCP client, which fixes its headers for initialize, list, call and session cleanup
  (`facades/_memory_client.py:173`, `facades/_mcp_session.py:28`); the A2A facade's long-lived client
  (`facades/a2a.py:40`); `Control._ek` for invocation-time context downloads (`control.py:508-529`);
  and hydrate (`harness/hydrate.py:341`, `:423`). The config schema accepts only
  `capability.ach.key = {"env": …}` (`config/schema.py:43`).
- Redaction of `ek-`/`ek_` tokens exists only in the run-failure formatter (`control.py:88-92`).
  Memory and A2A log exception text without it (`_memory_client.py:140`, `a2a.py:202`).

## 4. ACH side

### 4.1 Stable key per (identity, Environment, requester)

The exchange never mints per refresh. The first successful exchange for a tuple (identity,
Environment, requester ServiceAccount) creates one `ek_` with the existing mint logic; later
exchanges for the same tuple reuse it. The `environment_keys` row gains two columns:

- `issued_via` (`manual` | `exchange`), default `manual`;
- `requester` (`<namespace>/<serviceaccount>`), set only for `exchange`.

A partial unique index on `(owner_email, environment, requester)` where `issued_via = 'exchange'
AND status <> 'revoked'` makes concurrent first exchanges converge on one row: the loser of the
insert race revokes its fresh LiteLLM key and reads the winner's row.

Exchange keys do not count against `max_keys`. Their number is bounded by the grants, which only
platform admins write, at one per granted tuple. Key name `exchange:<namespace>/<serviceaccount>`.
LiteLLM metadata keeps `ach_issuer` (the base URL) and adds `ach_issued_via=exchange` and
`ach_requester`.

One stable key per tuple keeps one LiteLLM key, one continuous spend history and a meaningful
per-key budget (4.2, `spec.budget`). Removing a grant entry and adding it back revokes the key and
later creates a new one, so spend history and per-key budget start over; that is accepted.

### 4.2 `KeyGrant` CRD

Namespaced, in ACH's namespace. The operator projects it into a `key_grants` table, as it does
Environments.

```yaml
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-agent-k8s-doctor
  namespace: ach
spec:
  identity: ai.run@ackstorm.com          # exactly one email, lower-cased; no patterns
  environments: [agent-k8s-doctor]       # exact Environment names
  requesters:
    serviceAccounts:                     # exact "<namespace>/<name>"
      - ach-agent/ach-agent-control
  budget:                                # optional, per key this grant covers
    maxBudget: 120
    budgetDuration: 30d
status:
  conditions:
    - type: Ready
      status: "True"
```

- A grant allows every combination of its `environments` and its `requesters.serviceAccounts`.
  One grant per Environment, as above, lets each key carry its own budget.
- A request is allowed when one grant contains its identity, Environment and requester together;
  lists from different grants are never combined.
- Validation: `identity` is an email with no glob characters; every list has at least one exact
  entry; ServiceAccount entries are `<ns>/<name>`, both DNS-1123. `requesters` is a block so other
  requester kinds can be added later.
- `spec.budget` is written by the operator to the `key:<id>` budget of every exchange key the grant
  covers, so per-key budgets are declarative. `PATCH /platform/keys/{id}/budget` cannot do this: it
  only serves the key's owner (`envkeys/budget.go:73`). When several grants cover one tuple, the
  lowest `maxBudget` applies.
- `Ready=False` reasons: `IdentityUnknown`, `EnvironmentMissing`. The operator detects
  `IdentityUnknown` by asking LiteLLM `/user/info` for the identity. Teams are enrolled at login, so
  the identity (`ai.run@ackstorm.com`) must log in to ACH once by hand before its first exchange.

**Who may write grants.** Writing a `KeyGrant` is the power to act as its identity. The ACH chart:

- ships a ClusterRole `ach-keygrant-editor` (create, update, patch, delete on `keygrants`) bound
  to nobody by default;
- does not aggregate `keygrants` write verbs into the built-in `admin` or `edit` roles;
- documents that only the platform-admin group gets that binding, and that any wildcard role
  (`*` on `ach.ackstorm.ai` or on all resources) in ACH's namespace grants it too.

An RBAC test asserts that the `admin` and `edit` aggregations do not include `keygrants` writes.

### 4.3 Live authorization

The grant is checked at exchange time and again on every credential resolution. The JWT resolver
(4.5) loads the key row together with the projected grants and accepts it only while a grant still
allows its tuple. Removing a grant entry therefore cuts access within the resolver cache window
(60 s) without racing any mint.

The operator owns cleanup. It has a single leader, the LiteLLM master key and the hourly reaper.
When no grant allows a tuple any more, it revokes that tuple's exchange key with the existing revoke
path. Its RBAC adds `get/list/watch` on `keygrants`, `update` on `keygrants/status`, and the finalizer
it needs to remove projection rows on delete.

### 4.4 `POST /platform/keys/exchange`

The route is mounted outside the `pk_`/`ek_` authentication subtree. Authentication is
`Authorization: Bearer <ServiceAccount token>`; a `pk_`, `ek_` or ACH JWT is refused.

Request: `{"identity": "ai.run@ackstorm.com", "environment": "k8s-doctor"}`.

The route is reachable without credentials and every TokenReview is a call to the API server, so two
cheap guards come first: the bearer must be a compact JWS whose unverified `aud` contains the
exchange audience (else 401 `invalid_token`, no TokenReview), and requests are rate-limited per
client address and globally (429).

The exchange audience is `ACH_BASE_URL` with any trailing slash removed. The agent sets the projected
token's audience from `spec.ach.baseUrl`, normalised the same way, so `spec.ach.baseUrl` must equal
ACH's `ACH_BASE_URL`. Agents therefore exchange through ACH's public URL over HTTPS, which is how
they reach ACH today; reaching ACH through an internal Service is out of scope.

Checks, in order; the first failure answers:

| # | Check | Failure |
|---|---|---|
| 1 | TokenReview: `authenticated`; `status.audiences` contains the exchange audience (`<ACH_BASE_URL>`); user is `system:serviceaccount:<ns>:<name>`; `extra` carries `authentication.kubernetes.io/pod-name` and `pod-uid` (a pod-bound token, not one from `kubectl create token`) | 401 `invalid_token` |
| 2 | One grant contains identity, Environment and requester together | 403 `not_granted` |
| 3 | Environment exists and is synced (existing rule) | 404 / 503 `not_ready` |
| 4 | The identity's teams intersect `authorizedTeams` (existing rule) | 403 `unauthorized_team` |

A single `not_granted` code covers every grant miss, so a denial does not reveal which part of
which grant exists. The audit event records the detail.

On success: find or create the tuple's stable key (4.1), then sign a JWT with the existing Ed25519
signer:

- `iss` = `ACH_BASE_URL`, `aud` = `ach-ek` (distinct from the OAuth audience `ach`);
- `sub` = the key id (`ekid_…`);
- `exp` = now + 15 minutes, `iat`, `jti`.

Response `200`: `{"token": "<jwt>", "expires_in": 900, "key_id": "ekid_…"}`. The client schedules
from `expires_in`, never from wall-clock `exp`.

The mint logic is factored out of `mintAndInsert` so both handlers share it, while each writes its
own response and emits its own audit action.

**Residual exposure.** A JWT is a bearer token valid until `exp`; deleting the pod does not revoke a
JWT already issued. Exposure after a pod or grant is removed is bounded by 15 minutes for the JWT
and 60 seconds for the resolver cache. TokenReview itself accepts a deleted pod's token for up to 60
seconds (Kubernetes' pending-deletion allowance).

### 4.5 JWT resolution

A resolver modeled on `NewOAuthResolver` is added in the forwarder, content-service and platform API.
A compact JWS with audience `ach-ek` is verified with the shared signer, its `sub` is looked up as an
exchange key row, the live grant check (4.3) runs, and the row becomes the `KeyInfo` an `ek_` would
have produced.

Order is load-bearing. The OAuth resolver turns any JWS that fails verification against audience
`ach` into a 401 instead of falling through (`internal/keystore/oauthresolver.go`). The exchange
resolver therefore wraps the OAuth resolver: a JWS whose audience is not `ach-ek` goes to the OAuth
resolver unchanged, and a JWS with audience `ach-ek` that fails verification is a 401.

The JWT is accepted wherever an `ek_` is accepted today, in `x-ach-key` and in `Authorization`
(where the forwarder already treats a JWS as an ACH credential), so `EkOwnerGate`, budgets and
LiteLLM routing are unchanged.

Because the backing key never changes during rotation, a new JWT does not affect any request already
authenticated: long streams and MCP sessions keep their upstream LiteLLM key.

### 4.6 Audit and logging

- New action `platform.ek.exchange`, one event per request: requester, pod name and uid from
  TokenReview, identity, Environment, key id, outcome, denial reason. Never the ServiceAccount token
  or the JWT.
- The API server stores TokenReview bodies (which contain the token) only under audit level
  `Request` or `RequestResponse`. The install docs require `Metadata` level for `tokenreviews`;
  EKS control-plane audit policy is checked during rollout.
- The exchange audience is derived, never hard-coded (4.4).
- ach's `CLAUDE.md` is updated: the platform API gains a cluster-wide `system:auth-delegator`
  binding (TokenReview) in addition to its Dex leg.

## 5. ach-agent side

### 5.1 API (breaking, v1alpha1)

`spec.ach.keySecretRef` moves under `spec.ach.keyFrom`, which holds exactly one of:

```yaml
spec:
  capabilityServer: ach
  ach:
    baseUrl: https://api.ackstorm.ai    # https required with tokenExchange
    environment: agent-k8s-doctor       # required with tokenExchange
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```

```yaml
    keyFrom:
      secretKeyRef: {name: k8s-doctor-ek, key: ek}
```

CEL validation: exactly one of `tokenExchange` and `secretKeyRef`; with `tokenExchange`,
`environment` is required and `baseUrl` must be `https://`.

### 5.2 Operator

For `tokenExchange` agents only:

- The control pod keeps today's ServiceAccount (the profile's `control.serviceAccountName`, else
  `ach-agent-control-<agent uid>`). In production every agent shares `ach-agent-control`, which
  already has S3 access through EKS Pod Identity; adding an agent needs no Terraform change.
- A projected token is mounted: audience `spec.ach.baseUrl` without a trailing slash (it must equal
  ACH's `ACH_BASE_URL`, 4.4), `expirationSeconds: 3600`, path
  `/var/run/secrets/ach/token`. The kubelet renews it.
- The workspace config carries `capability.ach.key = {"exchange": {"identity": …, "environment": …,
  "tokenPath": …}}`. The Python schema (`config/schema.py`), the vendored schemas, hydration input
  and the operator's Secret discovery (`render.go:491`) change together.
- No Secret is referenced, so rotation never rolls the pod.

**Trust boundary.** The requester is a ServiceAccount, and agents in a namespace share one. Any
pod running that ServiceAccount, and anyone who can create pods in the namespace, can exchange
every grant that names it. Separation between agents is therefore the namespace: agents that must
not reach each other's Environments go in different namespaces with different ServiceAccounts. The
install docs require a namespace dedicated to agents, where only Flux and the ach-agent operator can
create pods, StatefulSets and Agents.

Per-agent separation inside one namespace (for example by a pod label read through the pod binding
TokenReview returns) is out of scope for now.

### 5.3 Harness

- **`KeySource`.** One object holds the current credential and a generation number. Every consumer
  reads it per request: model and MCP facades, memory auth and the memory MCP client (including
  session initialize and cleanup), the A2A facade client, context downloads (`Control._ek`) and
  hydrate. Clients that fix headers at construction take a header callable instead.
- **Exchange.** Before hydrate, read the token file and exchange. A refusal fails boot with ACH's
  error code. Exchange requests follow no redirects.
- **Rotation.** Schedule the next exchange at 80% of `expires_in` on the monotonic clock. The swap
  replaces one reference: requests already sent keep their JWT, every request built afterwards
  carries the new one. The old JWT stays valid until it expires, and nothing is revoked.
- **Refresh failure.** Keep the current JWT and retry with exponential backoff, clamped so a retry
  always happens before expiry. If the JWT expires with no replacement, the agent reports not-ready
  until an exchange succeeds.
- **Rejected credential.** If ACH rejects the credential itself (the forwarder's authentication
  failure, not an upstream 401), all concurrent callers wait on one shared re-exchange and retry once
  with the new generation. Only requests ACH rejected before proxying are retried, so no request
  reaches the upstream twice.
- **Redaction.** One logging filter scrubs `ek-`/`ek_` tokens, compact JWS strings and the
  ServiceAccount token across the control process, replacing the single-formatter redaction.
  Exception text from memory and A2A goes through it.
- With `secretKeyRef`, behaviour is unchanged.

### 5.4 Metrics and alert

- `ach_agent_key_refresh_failures_total` and `ach_agent_key_expiry_timestamp_seconds`.
- Alert `ACHAgentKeyRefreshFailing`: refresh failures continuing past half the JWT lifetime, or the
  JWT expired. The threshold is relative to the lifetime, so a healthy agent never fires it.

### 5.5 Budgets

All agents granted `ai.run@ackstorm.com` share that user's LiteLLM user budget, so one runaway agent
can exhaust it for all. The Environment's pooled budget and the grant's per-key `spec.budget` contain
this. The GitOps rollout sets `spec.budget` on each agent's grant.

## 6. Tests

**Acceptance (the 24x7 requirement).** e2e on kind with the JWT lifetime lowered to 1 minute (an ACH
setting for tests): continuous model calls, MCP calls (with an MCP session open across rotations)
and one long stream held open throughout. Passes only with zero failed requests across at least
three rotations.

ACH:

- Exchange checks in order, with TokenReview faked: unbound token (no pod claims), wrong audience,
  non-ServiceAccount user, no grant, grant with the Environment but not the requester, two grants
  whose lists would only match when mixed, unsynced Environment, team mismatch.
- Stable key: concurrent first exchanges converge on one row; later exchanges reuse it; exchange keys
  do not count against `max_keys`.
- Live authorization: removing a grant entry makes the JWT and the key unusable within the cache
  window; the operator revokes the key; an exchange racing the removal never yields a usable
  credential.
- JWT: expired, unknown `kid`, `sub` of a manual key, revoked key. An exchange JWT is accepted by the
  exchange resolver and refused by the OAuth one, and an OAuth JWT the other way round, with the
  resolvers in their production order. An exchange JWT works in `x-ach-key` and in `Authorization`.
- Exchange guards: a non-JWS bearer or a JWS with a foreign `aud` gets 401 with no TokenReview call;
  the rate limit returns 429.
- Audience: `https://api.ackstorm.ai/` and `https://api.ackstorm.ai` match.
- Budget: `spec.budget` reaches the `key:<id>` budget of every covered key; the lowest of two grants
  wins.
- RBAC: `admin`/`edit` aggregations exclude `keygrants` writes.
- Audit: one `platform.ek.exchange` per outcome; no token or JWT in any field.
- `ach_issuer` unchanged on exchange keys; the orphan reaper keeps them.
- envtest: `KeyGrant` projection, status, finalizer.
- e2e on kind: real pod token, real TokenReview.

ach-agent:

- `KeySource`: every consumer listed in 5.3 picks up a new generation without restart, including an
  open memory MCP session and the A2A client.
- Refresh at 80% on the monotonic clock; backoff clamped before expiry; not-ready after expiry.
- Rejected credential: concurrent callers share one re-exchange; upstream 401s are not retried.
- Redaction: JWT, `ek_` and ServiceAccount token never appear in logs from any facade.
- Operator: projected token and `capability.ach.key.exchange` only for `tokenExchange`; the
  control ServiceAccount is unchanged; CEL rejects both or neither `keyFrom` fields and `http://`
  with `tokenExchange`.

## 7. Order of work and rollout

1. **ACH 1.1.0** (additive): `KeyGrant` CRD, projection, operator cleanup, exchange endpoint, JWT
   resolver in forwarder, content-service and platform API, schema migration for `issued_via` and
   `requester`, audit action, TokenReview binding, RBAC roles, `CLAUDE.md`.
2. **ach-agent 0.2.0** (breaking at 0.x): `keyFrom`, projected token, `KeySource` across all
   consumers, exchange and rotation, global redaction, metrics, alert.
3. **GitOps**, one commit: one `KeyGrant` per Environment for `ai.run@ackstorm.com`, requester
   `ach-agent/ach-agent-control`, each with `spec.budget`; the four agents switched to
   `keyFrom.tokenExchange` with their `environment` set; chart pin `0.2.x`. No Terraform change. `ai.run` has already logged in
   to ACH, so its grants become `Ready`. After the agents run on exchanged
   credentials, a second commit deletes the four `*-ek` ExternalSecrets, their Secrets Manager
   entries and Terraform variables, and revokes the old manual keys.

## 8. Out of scope

- Cross-cluster agents (OIDC/JWKS validation instead of TokenReview); the `requesters` block leaves
  room for it.
- Standalone mode: unchanged.
- Credentials for identities without an explicit grant.
- The existing non-atomic quota check on `POST /platform/keys`.
