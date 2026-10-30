# Environment keys by ServiceAccount token exchange

Status: design approved in conversation, 2026-10-30. Owner: ach-agent project. Touches two repos:
ACH (`ackstorm/ach`, the provider of the contract) and ach-agent (`ackstorm/ach-agent`, its client).

## 1. Goal

An agent in ACH mode needs an Environment key (`ek_`) for one identity and one Environment. Today a
person mints it by hand, stores it in Secrets Manager, and an ExternalSecret copies it into the
agent's namespace (`workloads/agents/achagents/k8s-doctor.yaml` in GitOps). That leaves a long-lived
key at rest in three places and a manual step per agent.

With this design the agent's control pod proves who it is with its Kubernetes ServiceAccount token,
and ACH returns a short-lived `ek_` that exists only in the pod's memory. ACH never pushes keys: the
pod asks again before expiry and swaps keys with no failed request.

## 2. Requirements

- **R1. Opt-in per identity.** A key is minted only for an identity a platform admin named
  explicitly. A human user's key (any employee, the CEO) is never mintable unless someone writes a
  grant for that exact identity. No wildcards or patterns anywhere in a grant.
- **R2. Three grant checks plus today's check.** Every mint verifies: the identity is granted; the
  Environment is granted for that identity; the requesting ServiceAccount is granted. Then the
  existing rule still applies: the identity's teams intersect the Environment's `authorizedTeams`.
- **R3. No key at rest.** The minted key is never written to etcd, Git, Secrets Manager or a log.
- **R4. Transparent rotation.** A 24x7 agent rotates keys with zero failed requests, including
  requests in flight and long streams.
- **R5. Auditable.** Every exchange, granted or denied, emits an audit event without the key value.
- **R6. The existing path stays.** An Agent can still read its key from a Secret
  (`keyFrom.secretKeyRef`), for agents outside ACH's cluster or for manual keys.

Identities are the existing ACH identities (emails). `ai.run@ackstorm.com` is a real Google
Workspace account with its own Drive, Calendar, Zoho and GitLab access; it is the agents' identity.
No new "service identity" type is introduced.

## 3. Facts this design relies on

ACH (v1.0.1):

- `POST /platform/keys` creates an `ek_` only for a `pk_` caller
  (`internal/platformapi/envkeys/handler.go:195` `CreateHandler`). Team check:
  `validateTeamMembership` (`handler.go:398`). Mint: `mintAndInsert` (`handler.go:476`), which makes
  the bearer, stores only its peppered hash, creates the backing LiteLLM virtual key and the
  `environment_keys` row (`internal/db/environment_keys.go`).
- The raw `ek_` is returned once and is not recoverable: rotation means mint new, revoke old.
- Quota: `UserKeyAllowance` (`internal/db/user_limits.go:34`) counts every non-revoked key of the
  owner, expired ones included, against `max_keys`.
- Environments are CRDs projected into Postgres by the operator; the platform API reads the
  projection (`handler.go:175`), not the Kubernetes API. All ACH CRDs are namespaced; the operator
  watches one namespace (`ACH_NAMESPACE`, `ach` in production).
- The forwarder validates an `ek_` once per request, in the `EkOwnerGate` middleware
  (`internal/forwarder/proxy/gate.go:20`), before proxying. Revoking a key does not cut a request
  or stream that already passed the gate.
- Audit events go through `audit.EmitAudit`; key actions are `platform.ek.*`
  (`internal/audit/events.go:49`).

ach-agent (v0.1.5):

- `Agent.spec.ach` has `baseUrl`, optional `environment`, required `keySecretRef`, `model`
  (`operator/api/v1alpha1/agent_types.go`, `AchServerSpec`).
- The operator passes the key as env `ACH_SECRET_IDENTITY` (`operator/internal/render/workspacev1.go:45`,
  `wsconfig.go:297`) and hashes referenced Secrets into the pod config hash
  (`operator/internal/controller/agent_controller.go:327`).
- The control pod's ServiceAccount is `ach-agent-control-<agent uid>` unless the profile sets one
  (`operator/internal/render/wsconfig.go:126`).
- The harness reads the key once at assembly and bakes it into static header dicts for the model,
  MCP and memory facades and for hydrate (`runtime/src/ach_runtime/harness/control.py:887-900`,
  `:927`, `:966`, `:1020`). Log redaction already matches any `ek-`/`ek_` token
  (`control.py:88`).

## 4. ACH side

### 4.1 `KeyGrant` CRD

Namespaced, in ACH's namespace (`ach`), where only platform admins can write. The operator projects
it into a `key_grants` table, the same way it projects Environments.

```yaml
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-agents
  namespace: ach
spec:
  identity: ai.run@ackstorm.com        # exactly one email, lower-cased; no patterns
  environments:                        # exact Environment names
    - k8s-doctor
    - observability-assistant
    - zohodesk-classifier
    - gitlab-reviewer
  requesters:
    serviceAccounts:                   # exact "<namespace>/<name>"
      - ach-agent/agent-k8s-doctor
      - ach-agent/agent-observability-assistant
      - ach-agent/agent-zohodesk-classifier
      - ach-agent/agent-gitlab-reviewer
  keyTTL: 24h                          # 1h to 168h
status:
  conditions:
    - type: Ready                      # False with a reason when the grant cannot be used
      status: "True"
```

Validation (CRD schema plus admission):

- `identity` matches an email pattern; `*`, `?` and any glob character are rejected.
- `environments` and `requesters.serviceAccounts` have at least one entry; each entry is an exact
  name (`<ns>/<name>` for ServiceAccounts, both parts DNS-1123).
- `requesters` is a block so another requester kind can be added later without migrating grants.
- Several grants may name the same identity; a request is allowed when any one grant satisfies all
  three grant checks on its own (no mixing of lists across grants).
- `Ready=False` reasons: `IdentityUnknown` (the identity never logged in, so no LiteLLM user and no
  teams exist yet), `EnvironmentMissing` (a listed Environment does not exist).

### 4.2 `POST /platform/keys/exchange`

Authentication: `Authorization: Bearer <ServiceAccount token>`. A `pk_` or `ek_` caller is refused.

Request: `{"identity": "ai.run@ackstorm.com", "environment": "k8s-doctor"}`.

Checks, in order; the first failure answers:

| # | Check | Failure |
|---|---|---|
| 1 | TokenReview accepts the token with audience `https://api.ackstorm.ai` (the token is bound to a live pod; a deleted pod's token fails) | 401 `invalid_token` |
| 2 | A grant names `identity` | 403 `identity_not_granted` |
| 3 | That grant lists `environment` | 403 `environment_not_granted` |
| 4 | That grant lists the token's `<namespace>/<serviceaccount>` | 403 `requester_not_granted` |
| 5 | The identity's teams intersect the Environment's `authorizedTeams` (existing rule) | 403 `not_authorized_for_environment` |
| 6 | Environment exists, not terminating, access group synced (existing rule) | 404 / 409 as `POST /platform/keys` |
| 7 | Quota (`max_keys`) not reached (existing rule) | 403 `key_limit_reached` |

Steps 2-4 are evaluated per grant: the request passes if one grant satisfies all three. The error
reported is the furthest step any grant reached, so a denial says what is missing without listing
other grants.

On success the handler reuses `mintAndInsert` with:

- owner = the granted identity, provisioned in LiteLLM the same way as today;
- `expires_at` = now + `keyTTL`;
- name `exchange:<namespace>/<serviceaccount>`;
- metadata `ach_issuer=exchange`, `ach_requester=<namespace>/<serviceaccount>`, `ach_grant=<grant name>`.

Response `200`: `{"key": "ek-…", "key_id": "ekid_…", "expires_at": "…"}`.

**One live key per requester.** After a successful exchange, every earlier non-revoked key with the
same (requester, identity, environment) is scheduled for revocation 10 minutes later. The schedule
is a `revoke_after` timestamp on the key row, swept by the platform API every minute; the sweep uses
the existing revoke path (LiteLLM first, then the row). This bounds the quota cost to about two keys
per agent and keeps restarts from accumulating keys.

**Grant removal.** When a grant is deleted, or an identity, Environment or requester disappears from
it, every key minted under that grant for the removed entries is revoked immediately.

**Expired keys.** The sweep also revokes exchange-issued keys past `expires_at`, so expired keys
stop counting against the quota.

### 4.3 Audit

New action `platform.ek.exchange`. One event per request, success or denial, with requester,
identity, environment, grant, key id (on success), outcome and the error code. Never the key, never
the token.

### 4.4 Permissions

- Platform API ServiceAccount: ClusterRoleBinding to `system:auth-delegator` (TokenReview).
- Operator: watch `keygrants` in its namespace (same scope as Environments).
- No Kubernetes write permission is added anywhere.

## 5. ach-agent side

### 5.1 API (breaking, v1alpha1)

`spec.ach.keySecretRef` moves under `spec.ach.keyFrom`, which holds exactly one of:

```yaml
spec:
  capabilityServer: ach
  ach:
    baseUrl: https://api.ackstorm.ai
    environment: k8s-doctor            # required with tokenExchange
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```

```yaml
    keyFrom:
      secretKeyRef: {name: k8s-doctor-ek, key: ek}
```

CEL validation: exactly one of `tokenExchange` and `secretKeyRef`; `environment` required with
`tokenExchange`.

### 5.2 Operator

- The default control ServiceAccount becomes `agent-<agent name>` instead of
  `ach-agent-control-<agent uid>`, for every Agent: created and owned by the Agent, and stable
  across Agent re-creation so a grant can name it. A profile's `control.serviceAccountName` still
  wins; the grant must then name that ServiceAccount.
- It mounts a projected token: audience `https://api.ackstorm.ai`, `expirationSeconds: 3600`, path
  `/var/run/secrets/ach/token`. The kubelet renews the file.
- The workspace config's `capability.ach.key` becomes either `{"env": "ACH_SECRET_IDENTITY"}` (as
  today) or `{"exchange": {"identity": "…", "environment": "…", "tokenPath": "…"}}`.
- No Secret is referenced, so key rotation never rolls the pod.

### 5.3 Harness

- A `KeySource` holds the current key. Facades, hydrate and memory auth read it per request instead
  of the static header dicts built at assembly. `upstream_secrets` (redaction) covers every key the
  source has held.
- With `exchange`, the harness exchanges before hydrate, reading the token file fresh each time.
  Boot fails with the ACH error code if the exchange is refused.
- **Rotation.** At 80% of the key's lifetime it exchanges again. The swap replaces one reference:
  requests already sent keep the old key and finish with it, every request built afterwards carries
  the new one. ACH revokes the old key 10 minutes later (4.2), after every request that started with
  it has passed the gate.
- **Refresh failure.** Keep the current key, retry with exponential backoff (cap 5 min), expose
  `ach_agent_key_refresh_failures_total` and the current key's expiry as
  `ach_agent_key_expiry_timestamp_seconds`. With TTL 24h the margin is about 4.8 hours.
- **Revoked key.** A 401 from ACH on a facade request, before any response byte reached the engine,
  triggers at most one immediate re-exchange per minute and one retry of that request.
- With `env`, behaviour is unchanged.

### 5.4 Chart and alerts

New alert `ACHAgentKeyRefreshFailing`: refresh failures for 15 minutes, or key expiry under 1 hour.

## 6. Tests

**Acceptance (the 24x7 requirement).** e2e on kind with `keyTTL: 2m`: continuous model calls, MCP
calls and one long stream held open throughout. Passes only with zero failed requests across at
least three rotations, and the long stream survives the revocation of the key it started with.

ACH:

- Unit, with TokenReview faked: each check and its error in order; per-grant evaluation (lists from
  two grants are not mixed); CEO-style identity with no grant; unlisted Environment; foreign
  ServiceAccount; dead-pod token; `pk_`/`ek_` caller refused.
- Schema: wildcard identity, glob characters, empty lists, malformed `<ns>/<name>` rejected.
- Revocation: earlier keys revoked after the overlap, not before; grant edits revoke affected keys;
  expired keys swept.
- Quota: exchange counts against `max_keys`; the sweep frees slots.
- Audit: one `platform.ek.exchange` event per outcome; no key or token value in any field.
- envtest: `KeyGrant` projection into `key_grants`.
- e2e on kind: real pod token, real TokenReview.

ach-agent:

- Harness: refresh scheduled at 80%; swap under concurrent requests (no request leaves without a
  key, none mixes keys); failed refresh keeps the key and retries; 401 triggers one re-exchange and
  one retry; redaction covers old and new keys.
- Operator: render of `agent-<name>` ServiceAccount and projected token; `keyFrom` exactly-one
  validation; `secretKeyRef` path renders as before.

## 7. Order of work and rollout

1. **ACH 1.1.0** (additive): `KeyGrant` CRD and projection, exchange endpoint, revocation sweep,
   audit action, TokenReview permission in the chart.
2. **ach-agent 0.2.0** (breaking at 0.x): `keyFrom`, ServiceAccount and projected token, harness
   `KeySource`, exchange, rotation, metrics, alert.
3. **GitOps**, one commit: `KeyGrant` for `ai.run@ackstorm.com`; the four agents switched to
   `keyFrom.tokenExchange`; `ai.run`'s `max_keys` raised to cover two keys per agent. After the
   agents run on exchanged keys, a second commit deletes the four `*-ek` ExternalSecrets, their
   Secrets Manager entries and Terraform variables, and revokes the old manual keys.

## 8. Out of scope

- Cross-cluster agents (OIDC/JWKS validation instead of TokenReview). The `requesters` block leaves
  room for it.
- Standalone mode: unchanged.
- Minting keys for humans or for identities without an explicit grant.
