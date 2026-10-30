# ACH 1.1.0 — Environment credentials by ServiceAccount token exchange (Plan A, ACH side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ACH 1.1.0: a `KeyGrant` CRD and `POST /platform/keys/exchange`, which trades a pod-bound Kubernetes ServiceAccount token for a short-lived ACH JWT standing for one stable `ek_` that never leaves ACH, plus resolution of that JWT in the forwarder, content service and platform API.

**Architecture:** The operator projects `KeyGrant` CRs into a `key_grants` table and owns cleanup (revoke exchange keys no grant allows, keep per-key budgets at the lowest covering `spec.budget`). The platform API's exchange handler (outside the `pk_`/`ek_` Authn subtree) runs cheap guards, TokenReview, the grant check and the existing Environment/team checks, then finds or mints the tuple's stable `ek_` through the create path's factored `mint` and signs an `aud=ach-ek` JWT with the shared Ed25519 signer. A new `keystore` resolver wraps the OAuth resolver inside the Redis cache and maps that JWT to the exchange key row while a grant still allows it. One rule (`internal/keygrant`) decides "a grant allows this tuple" for the handler, the resolver and the operator.

**Tech Stack:** Go 1.26, chi, pgx v5 + golang-migrate, controller-runtime 0.24 / envtest, client-go (typed `authentication/v1` client), golang.org/x/time/rate, golang-jwt v5 (EdDSA), Helm, kind e2e (stdlib `testing`, build tag `e2e`).

**Requirements source:** `/home/coder/workspace/local/ach-agent/docs/superpowers/specs/2026-10-30-ek-token-exchange-design.md` §1–4, §6 (ACH tests), §7.1. §5 (client side) is Plan B; the wire contract below is shared with it.

**Where to work:** an isolated git worktree of `/home/coder/workspace/local/ach` (superpowers:using-git-worktrees). The primary checkout is in use by another session: never build, run `make`, or change git state there. Each worktree gets its own `.gocache`; run `make clean-cache` in the worktree when done.

## Global Constraints

- Host has no Go toolchain: every Go command runs through a `make` target (auto-routed into `ach-devtools`) or `./scripts/dev.sh <cmd>`. Generator targets (`gen-code`, `gen-manifests`, `gen-crd-ref-docs`, `helm-sync`) are NOT auto-routed: run them as `./scripts/dev.sh make <target>`.
- Every command is foreground and wrapped in `timeout N`. Gates write to a log file and the exit status is read separately: `LOG=$(mktemp); timeout N make X > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`. Never pipe a gate into `tail`/`grep` and trust the exit code. No unbounded polling loops.
- Every new `*.go` and `*.sql` file starts with `// SPDX-License-Identifier: Apache-2.0` (`-- SPDX-License-Identifier: Apache-2.0` for SQL). Core code never imports `ee/`.
- Comments state what the code does now and why. No history, no process labels (phase/plan/task/decision IDs), no "new/now" narration.
- Documentation hygiene (ACH `AGENTS.md`): a change that alters a contract updates `AGENTS.md` / `references/` / `docs/` in the SAME commit.
- Exchange audience = `ACH_BASE_URL` with any trailing slash removed, derived at runtime, never hard-coded.
- Exchange request: `POST /platform/keys/exchange`, `Authorization: Bearer <ServiceAccount token>`, body `{"identity": "<email>", "environment": "<name>"}` (unknown fields refused). A `pk_`, `ek_` or ACH JWT bearer is refused (401 `invalid_token`).
- Exchange response `200`: `{"token": "<jwt>", "expires_in": <seconds>, "key_id": "ekid_…"}`.
- Exchange JWT: `iss` = `ACH_BASE_URL` (trailing slash removed), `aud` = `ach-ek`, `sub` = the key id, `exp` = now + TTL, `iat`, `jti`. TTL default 15 minutes, chart `platformApi.keyExchange.tokenTTL` → `ACH_KEY_EXCHANGE_TOKEN_TTL`, minimum 1 minute (the 1-minute floor is the acceptance-test setting).
- Error envelope (existing `render.Error`): `{"error":{"code","message"},"request_id"}`. Codes: 400 `invalid_argument`; 401 `invalid_token` (guard or TokenReview); 429 `rate_limited`; 403 `not_granted` (one code for every grant miss); 404 `environment_not_found`; 503 `not_ready` (Environment unsynced or TokenReview unavailable); 403 `unauthorized_team`; 409 `key_suspended`; 502/503 `litellm_rejected`/`litellm_unreachable`; 500 `internal_error`/`db_insert_failed`.
- Check order (first failure answers): guard (compact JWS whose unverified `aud` contains the audience) → rate limit (per client address, then global) → body → TokenReview (authenticated, `status.audiences` contains the audience, user `system:serviceaccount:<ns>:<name>`, extras `authentication.kubernetes.io/pod-name` and `pod-uid`) → one grant names identity + Environment + requester together → Environment exists and synced → identity's teams ∩ `authorizedTeams`.
- One stable key per (identity, Environment, `<ns>/<sa>`): `environment_keys.issued_via='exchange'`, `requester='<ns>/<sa>'`, name `exchange:<ns>/<sa>`; partial unique index on `(owner_email, environment, requester) WHERE issued_via='exchange' AND status<>'revoked'`; the insert-race loser revokes its fresh LiteLLM key and answers with the winner's row. LiteLLM metadata keeps `ach_issuer` (= `ACH_BASE_URL` as configured) and adds `ach_issued_via=exchange`, `ach_requester`.
- Exchange keys never count against `max_keys`.
- Grants: no wildcards or patterns anywhere; a request is allowed only when ONE grant contains its identity, Environment and requester; lowest `maxBudget` wins when several grants cover a key.
- The JWT is accepted wherever an `ek_` is (`x-ach-key` and `Authorization: Bearer`) in forwarder, content service and platform API, only while a grant still allows its tuple. The exchange resolver wraps the OAuth resolver; both sit inside the 60-second Redis cache.
- No `ek_`, JWT or ServiceAccount token in etcd, Git, logs, audit records or metrics. One `platform.ek.exchange` audit event per request, granted or denied.
- Platform API gains a cluster-wide `system:auth-delegator` ClusterRoleBinding. Chart ships ClusterRole `ach-keygrant-editor` (release `ach`) bound to nobody; no chart role aggregates `keygrants` writes into `admin`/`edit`.
- Release: ACH 1.1.0, additive. Migration number `000026`.

---

## File Structure

| Path (ACH repo) | Responsibility | Task |
|---|---|---|
| `db/migrations/000026_key_exchange.{up,down}.sql` | `issued_via`/`requester` columns, exchange-tuple unique index, `key_grants` table | 1 |
| `internal/db/key_grants.go` | `KeyGrantRow` + upsert/delete/list helpers | 1 |
| `internal/db/environment_keys.go`, `types_keys.go`, `user_limits.go` | insert columns, `FindExchangeKey`, `ExchangeKeyResolve`, `ListExchangeKeys`, quota exclusion | 1 |
| `internal/keygrant/keygrant.go` | THE grant rule: `Covering`, `Miss`, `Budget`, `Plan` | 1 |
| `internal/keys/keys.go` | `PeekAudience` (unverified `aud`, routing only) | 2 |
| `internal/forwarder/jwt/{verify,signer}.go` | `VerifyExp`; optional `jti` claim | 2 |
| `internal/keystore/exchangeresolver.go`, `dbresolver.go` | exchange-JWT resolver; shared `keyInfoFromEK` | 2 |
| `cmd/ach/cmd/{forwarder,content_service,platform_api}.go` | resolver chain wiring | 2 |
| `internal/platformapi/envkeys/handler.go` | `createReq` carries audit action; `mint` factored out of `mintAndInsert`; exchange insert/race branch | 3, 4 |
| `internal/platformapi/envkeys/exchange.go` | exchange handler, TokenReview check, rate limiter | 4 |
| `internal/audit/events.go` | `platform.ek.exchange` + outcomes | 4 |
| `internal/platformapi/{server,adapters}.go`, `middleware/middleware.go` | route outside Authn; DB adapter; exported `ClientIP` | 4 |
| `cmd/ach/cmd/tokenreview.go` | in-cluster TokenReview client | 4 |
| `deploy/helm/ach/templates/platform-api-{rbac,deployment}.yaml`, `values.yaml` | auth-delegator binding; TTL value | 4 |
| `api/ach/v1alpha1/keygrant_types.go` (+ generated deepcopy, CRD, chart copy, role.yaml, API ref) | `KeyGrant` CRD | 5 |
| `internal/controller/ach/keygrant_controller.go` | projection, status, finalizer, cleanup sweep | 5 |
| `internal/operator/resync/runnable.go`, `cmd/ach/cmd/operator.go` | resync channel + reconciler wiring | 5 |
| `deploy/helm/ach/templates/{operator-rbac,keygrant-editor-role}.yaml`, `scripts/helm-render-check.sh` | operator RBAC, editor role, no-aggregation check | 6 |
| `docs/user-guide/key-exchange.md`, `mkdocs.yml`, `docs/getting-started/installation.md`, `examples/keygrant.yaml` | operator-facing docs | 6 |
| `test/e2e/key_exchange_test.go`, `test/e2e/README.md` | kind e2e: real pod token, real TokenReview | 7 |
| `CHANGELOG.md` | 1.1.0 entry, release commit | 8 |

---

### Task 1: Exchange data model — migration, DB helpers, the grant rule

**Files:**
- Create: `db/migrations/000026_key_exchange.up.sql`, `db/migrations/000026_key_exchange.down.sql`
- Create: `internal/db/key_grants.go`
- Modify: `internal/db/types_keys.go` (`EkKeyInfo`, `EkInsertRow`)
- Modify: `internal/db/environment_keys.go:44-63` (`InsertEnvironmentKey`) and append three helpers
- Modify: `internal/db/user_limits.go:27-38` (`UserKeyAllowance`)
- Create: `internal/keygrant/keygrant.go`, `internal/keygrant/keygrant_test.go`
- Test: `internal/db/key_exchange_test.go` (build tag `integration`, testcontainers Postgres)

**Interfaces:**
- Produces (package `db`):
  - `type KeyGrantRow struct { Namespace, Name, Identity string; Environments, ServiceAccounts []string; MaxBudget *float64; BudgetDuration string }`
  - `UpsertKeyGrant(ctx, *pgxpool.Pool, KeyGrantRow) error`, `DeleteKeyGrant(ctx, *pgxpool.Pool, ns, name string) error`, `ListKeyGrants(ctx, *pgxpool.Pool, ns string) ([]KeyGrantRow, error)`, `ListKeyGrantsForIdentity(ctx, *pgxpool.Pool, ns, identity string) ([]KeyGrantRow, error)`
  - `const ExchangeTupleConstraint = "environment_keys_exchange_tuple_uniq"`
  - `EkKeyInfo.Requester string`; `EkInsertRow.IssuedVia string` (`""` = manual), `EkInsertRow.Requester *string`
  - `FindExchangeKey(ctx, *pgxpool.Pool, owner, environment, requester string) (*EkKeyInfo, error)` — live (non-revoked) row or `(nil, nil)`
  - `ExchangeKeyResolve(ctx, *pgxpool.Pool, keyID string) (*EkKeyInfo, error)` — active, unexpired exchange row with `Requester` and `LiteLLMKeyMaterial`, or `(nil, nil)`
  - `ListExchangeKeys(ctx, *pgxpool.Pool) ([]EkKeyInfo, error)` — non-revoked exchange rows (`KeyID, Environment, OwnerEmail, Requester, LiteLLMToken`)
- Produces (package `keygrant`):
  - `Covering(grants []db.KeyGrantRow, identity, env, requester string) []db.KeyGrantRow`
  - `Miss(grants []db.KeyGrantRow, env string) string` — `no_grant_for_identity` | `environment_not_granted` | `requester_not_granted` (grants already filtered to one identity)
  - `Budget(covering []db.KeyGrantRow) *litellm.TagBudget` — lowest `MaxBudget`, nil when none sets one
  - `Plan(grants []db.KeyGrantRow, keys []db.EkKeyInfo) (revoke []db.EkKeyInfo, budgets map[string]litellm.TagBudget)`

- [ ] **Step 1: Write the failing unit test for the grant rule**

`internal/keygrant/keygrant_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package keygrant

import (
	"testing"

	"github.com/ackstorm/ach/internal/db"
)

const id = "ai.run@ackstorm.com"

func grant(name string, envs, sas []string, budget *float64, duration string) db.KeyGrantRow {
	return db.KeyGrantRow{Namespace: "ach", Name: name, Identity: id,
		Environments: envs, ServiceAccounts: sas, MaxBudget: budget, BudgetDuration: duration}
}

func TestCovering_OneGrantMustNameAllThree(t *testing.T) {
	grants := []db.KeyGrantRow{
		grant("a", []string{"prod"}, []string{"agents/other"}, nil, ""),
		grant("b", []string{"dev"}, []string{"agents/doctor"}, nil, ""),
	}
	if got := Covering(grants, id, "prod", "agents/doctor"); len(got) != 0 {
		t.Fatalf("lists from two grants were combined: %v", got)
	}
	grants = append(grants, grant("c", []string{"prod"}, []string{"agents/doctor"}, nil, ""))
	if got := Covering(grants, id, "prod", "agents/doctor"); len(got) != 1 || got[0].Name != "c" {
		t.Fatalf("want only grant c, got %v", got)
	}
	if got := Covering(grants, "ceo@ackstorm.com", "prod", "agents/doctor"); len(got) != 0 {
		t.Fatalf("an identity no grant names was allowed: %v", got)
	}
}

func TestMiss(t *testing.T) {
	cases := []struct {
		name   string
		grants []db.KeyGrantRow
		want   string
	}{
		{"none", nil, "no_grant_for_identity"},
		{"environment", []db.KeyGrantRow{grant("a", []string{"dev"}, []string{"agents/doctor"}, nil, "")}, "environment_not_granted"},
		{"requester", []db.KeyGrantRow{grant("a", []string{"prod"}, []string{"agents/other"}, nil, "")}, "requester_not_granted"},
	}
	for _, tc := range cases {
		if got := Miss(tc.grants, "prod"); got != tc.want {
			t.Errorf("%s: got %q want %q", tc.name, got, tc.want)
		}
	}
}

func TestBudget_LowestWins(t *testing.T) {
	fifty, twenty := 50.0, 20.0
	if Budget([]db.KeyGrantRow{grant("a", nil, nil, nil, "")}) != nil {
		t.Fatal("no grant sets a budget: want nil")
	}
	b := Budget([]db.KeyGrantRow{
		grant("a", nil, nil, &fifty, "30d"),
		grant("b", nil, nil, nil, ""),
		grant("c", nil, nil, &twenty, "7d"),
	})
	if b == nil || b.MaxBudget != 20 || b.BudgetDuration != "7d" {
		t.Fatalf("want 20/7d, got %+v", b)
	}
}

func TestPlan(t *testing.T) {
	ten := 10.0
	grants := []db.KeyGrantRow{grant("a", []string{"prod"}, []string{"agents/doctor"}, &ten, "30d")}
	keys := []db.EkKeyInfo{
		{KeyID: "ekid_keep", OwnerEmail: id, Environment: "prod", Requester: "agents/doctor"},
		{KeyID: "ekid_gone", OwnerEmail: id, Environment: "prod", Requester: "agents/removed"},
	}
	revoke, budgets := Plan(grants, keys)
	if len(revoke) != 1 || revoke[0].KeyID != "ekid_gone" {
		t.Fatalf("revoke = %v", revoke)
	}
	if b, ok := budgets["ekid_keep"]; !ok || b.MaxBudget != 10 || len(budgets) != 1 {
		t.Fatalf("budgets = %v", budgets)
	}
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/keygrant > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|undefined' "$LOG"`
Expected: FAIL — `undefined: Covering` (and `db.KeyGrantRow`, `EkKeyInfo.Requester`).

- [ ] **Step 3: Write the migration**

`db/migrations/000026_key_exchange.up.sql`:

```sql
-- SPDX-License-Identifier: Apache-2.0

-- 000026: ServiceAccount token exchange (POST /platform/keys/exchange).
--
-- environment_keys records how a key was issued and, for an exchange key, who
-- asked for it ("<namespace>/<serviceaccount>"). One live exchange key per
-- (owner, environment, requester): the partial unique index makes concurrent
-- first exchanges converge — the insert loser revokes its LiteLLM key and
-- reads the winner's row.
--
-- key_grants is the operator's projection of KeyGrant CRs. The exchange
-- handler and the exchange-JWT resolvers query it per request (resolvers
-- behind the 60 s cache), so it has no NOTIFY channel and no reader cache.
ALTER TABLE environment_keys
    ADD COLUMN issued_via text NOT NULL DEFAULT 'manual',
    ADD COLUMN requester  text NULL,
    ADD CONSTRAINT environment_keys_issued_via_enum CHECK (issued_via IN ('manual','exchange')),
    ADD CONSTRAINT environment_keys_requester_chk CHECK ((issued_via = 'exchange') = (requester IS NOT NULL));

CREATE UNIQUE INDEX environment_keys_exchange_tuple_uniq
    ON environment_keys (owner_email, environment, requester)
    WHERE issued_via = 'exchange' AND status <> 'revoked';

CREATE TABLE key_grants (
    namespace        text             NOT NULL,
    name             text             NOT NULL,
    identity         text             NOT NULL,
    environments     text[]           NOT NULL,
    service_accounts text[]           NOT NULL,
    max_budget       double precision NULL,
    budget_duration  text             NOT NULL DEFAULT '',
    updated_at       timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, name)
);

CREATE INDEX key_grants_identity_idx ON key_grants (namespace, identity);
```

`db/migrations/000026_key_exchange.down.sql`:

```sql
-- SPDX-License-Identifier: Apache-2.0

-- Refused while a live exchange key exists: dropping issued_via would turn it
-- into a manual key that counts against max_keys and that no grant governs.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM environment_keys WHERE issued_via = 'exchange' AND status <> 'revoked') THEN
        RAISE EXCEPTION 'environment_keys has live exchange keys; revoke them before rolling back 000026';
    END IF;
END $$;
DROP TABLE key_grants;
DROP INDEX environment_keys_exchange_tuple_uniq;
ALTER TABLE environment_keys
    DROP CONSTRAINT environment_keys_requester_chk,
    DROP CONSTRAINT environment_keys_issued_via_enum,
    DROP COLUMN requester,
    DROP COLUMN issued_via;
```

- [ ] **Step 4: Extend the key row types**

In `internal/db/types_keys.go`, add to `EkKeyInfo` after `ExpiresAt *time.Time`:

```go
	// Requester is "<namespace>/<serviceaccount>" for an exchange key
	// (migration 000026). Empty for a manual key and for read paths that do
	// not select it.
	Requester string
```

and to `EkInsertRow` after `ExpiresAt *time.Time`:

```go
	// IssuedVia is "exchange" for a key minted by POST /platform/keys/exchange;
	// empty means "manual". Requester is set only for an exchange key (a CHECK
	// constraint refuses any other combination).
	IssuedVia string
	Requester *string
```

- [ ] **Step 5: Write the DB helpers**

Replace `InsertEnvironmentKey` in `internal/db/environment_keys.go` with:

```go
// ExchangeTupleConstraint is the partial unique index (migration 000026) that
// admits one live exchange key per (owner, environment, requester).
const ExchangeTupleConstraint = "environment_keys_exchange_tuple_uniq"

// InsertEnvironmentKey writes a single environment_keys row. status defaults
// to 'active' and created_at defaults to now(); an empty IssuedVia is 'manual'.
//
// Per D-12 step 7, the handler is responsible for running the LiteLLM
// compensation (RevokeKey) when this returns a non-transient error.
func InsertEnvironmentKey(ctx context.Context, pool *pgxpool.Pool, row EkInsertRow) error {
	const sql = `
		INSERT INTO environment_keys
		    (key_id, credential_hash, environment, owner_email, name,
		     status, litellm_user_id, litellm_token, litellm_key_material_enc, expires_at,
		     issued_via, requester)
		VALUES ($1, $2, $3, $4, $5, 'active', $6, $7, $8, $9, $10, $11)
	`
	if _, err := pool.Exec(ctx, sql,
		row.KeyID, row.CredentialHash, row.Environment, row.OwnerEmail, row.Name,
		row.LiteLLMUserID, row.LiteLLMToken, row.LiteLLMKeyMaterial, row.ExpiresAt,
		cmp.Or(row.IssuedVia, "manual"), row.Requester,
	); err != nil {
		if isTransientPgErr(err) {
			return err
		}
		return fmt.Errorf("db: InsertEnvironmentKey(%s): %w", row.KeyID, err)
	}
	return nil
}
```

(add `"cmp"` to the imports), and append to the same file:

```go
// FindExchangeKey returns the live (non-revoked) exchange key of one
// (owner, environment, requester) tuple, or (nil, nil).
func FindExchangeKey(ctx context.Context, pool *pgxpool.Pool, owner, environment, requester string) (*EkKeyInfo, error) {
	const sql = `
		SELECT key_id, credential_hash, environment, owner_email, name,
		       litellm_user_id, litellm_token,
		       status, created_at, last_used_at, revoked_at, expires_at, requester
		  FROM environment_keys
		 WHERE issued_via = 'exchange' AND status <> 'revoked'
		   AND owner_email = $1 AND environment = $2 AND requester = $3
	`
	r := &EkKeyInfo{}
	err := pool.QueryRow(ctx, sql, owner, environment, requester).Scan(
		&r.KeyID, &r.CredentialHash, &r.Environment, &r.OwnerEmail, &r.Name,
		&r.LiteLLMUserID, &r.LiteLLMToken,
		&r.Status, &r.CreatedAt, &r.LastUsedAt, &r.RevokedAt, &r.ExpiresAt, &r.Requester,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: FindExchangeKey: %w", err)
	}
	return r, nil
}

// ExchangeKeyResolve is EkResolve for an exchange JWT: the row is found by the
// JWT's subject (key_id), not by a credential hash, and only an active,
// unexpired exchange key matches — a manual key's id never resolves here.
// last_used_at is debounced exactly like EkResolve.
func ExchangeKeyResolve(ctx context.Context, pool *pgxpool.Pool, keyID string) (*EkKeyInfo, error) {
	const sql = `
		UPDATE environment_keys SET
		    last_used_at = CASE
		        WHEN last_used_at IS NULL
		          OR last_used_at < now() - interval '5 minutes'
		        THEN now()
		        ELSE last_used_at
		    END
		 WHERE key_id = $1
		   AND issued_via = 'exchange'
		   AND status = 'active'
		   AND (expires_at IS NULL OR expires_at > now())
		RETURNING key_id, environment, owner_email, name,
		          litellm_user_id, litellm_token, litellm_key_material_enc, expires_at, requester
	`
	r := &EkKeyInfo{}
	err := pool.QueryRow(ctx, sql, keyID).Scan(
		&r.KeyID, &r.Environment, &r.OwnerEmail, &r.Name,
		&r.LiteLLMUserID, &r.LiteLLMToken, &r.LiteLLMKeyMaterial, &r.ExpiresAt, &r.Requester,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: ExchangeKeyResolve: %w", err)
	}
	return r, nil
}

// ListExchangeKeys returns every non-revoked exchange key; the operator's
// KeyGrant sweep revokes the ones no grant allows any more.
func ListExchangeKeys(ctx context.Context, pool *pgxpool.Pool) ([]EkKeyInfo, error) {
	rows, err := pool.Query(ctx, `
		SELECT key_id, environment, owner_email, requester, litellm_token
		  FROM environment_keys
		 WHERE issued_via = 'exchange' AND status <> 'revoked'
		 ORDER BY key_id`)
	if err != nil {
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: ListExchangeKeys: %w", err)
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (EkKeyInfo, error) {
		var k EkKeyInfo
		err := r.Scan(&k.KeyID, &k.Environment, &k.OwnerEmail, &k.Requester, &k.LiteLLMToken)
		return k, err
	})
	if err != nil {
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: ListExchangeKeys: %w", err)
	}
	return out, nil
}
```

`internal/db/key_grants.go`:

```go
// SPDX-License-Identifier: Apache-2.0

// Package db helpers for the key_grants projection (KeyGrant CRs, migration
// 000026). The operator is the only writer; the exchange handler and the
// exchange-JWT resolvers read it per request.

package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// KeyGrantRow is one projected KeyGrant. Identity is lower-case (the CRD
// admits nothing else). MaxBudget is nil when the grant sets no budget.
type KeyGrantRow struct {
	Namespace       string
	Name            string
	Identity        string
	Environments    []string
	ServiceAccounts []string
	MaxBudget       *float64
	BudgetDuration  string
}

// UpsertKeyGrant writes the row of one KeyGrant CR.
func UpsertKeyGrant(ctx context.Context, pool *pgxpool.Pool, row KeyGrantRow) error {
	const sql = `
		INSERT INTO key_grants
		    (namespace, name, identity, environments, service_accounts, max_budget, budget_duration, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, now())
		ON CONFLICT (namespace, name) DO UPDATE SET
		    identity         = EXCLUDED.identity,
		    environments     = EXCLUDED.environments,
		    service_accounts = EXCLUDED.service_accounts,
		    max_budget       = EXCLUDED.max_budget,
		    budget_duration  = EXCLUDED.budget_duration,
		    updated_at       = now()
	`
	if _, err := pool.Exec(ctx, sql, row.Namespace, row.Name, row.Identity,
		row.Environments, row.ServiceAccounts, row.MaxBudget, row.BudgetDuration); err != nil {
		if isTransientPgErr(err) {
			return err
		}
		return fmt.Errorf("db: UpsertKeyGrant(%s/%s): %w", row.Namespace, row.Name, err)
	}
	return nil
}

// DeleteKeyGrant removes the row; an absent row is not an error.
func DeleteKeyGrant(ctx context.Context, pool *pgxpool.Pool, ns, name string) error {
	if _, err := pool.Exec(ctx, `DELETE FROM key_grants WHERE namespace = $1 AND name = $2`, ns, name); err != nil {
		if isTransientPgErr(err) {
			return err
		}
		return fmt.Errorf("db: DeleteKeyGrant(%s/%s): %w", ns, name, err)
	}
	return nil
}

const keyGrantColumns = `namespace, name, identity, environments, service_accounts, max_budget, budget_duration`

// ListKeyGrants returns every grant in ns, ordered by name.
func ListKeyGrants(ctx context.Context, pool *pgxpool.Pool, ns string) ([]KeyGrantRow, error) {
	return queryKeyGrants(ctx, pool, "ListKeyGrants",
		`SELECT `+keyGrantColumns+` FROM key_grants WHERE namespace = $1 ORDER BY name`, ns)
}

// ListKeyGrantsForIdentity returns the grants in ns naming identity, ordered by name.
func ListKeyGrantsForIdentity(ctx context.Context, pool *pgxpool.Pool, ns, identity string) ([]KeyGrantRow, error) {
	return queryKeyGrants(ctx, pool, "ListKeyGrantsForIdentity",
		`SELECT `+keyGrantColumns+` FROM key_grants WHERE namespace = $1 AND identity = $2 ORDER BY name`, ns, identity)
}

func queryKeyGrants(ctx context.Context, pool *pgxpool.Pool, op, sql string, args ...any) ([]KeyGrantRow, error) {
	rows, err := pool.Query(ctx, sql, args...)
	if err != nil {
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: %s: %w", op, err)
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (KeyGrantRow, error) {
		var g KeyGrantRow
		err := r.Scan(&g.Namespace, &g.Name, &g.Identity, &g.Environments, &g.ServiceAccounts, &g.MaxBudget, &g.BudgetDuration)
		return g, err
	})
	if err != nil {
		if isTransientPgErr(err) {
			return nil, err
		}
		return nil, fmt.Errorf("db: %s: %w", op, err)
	}
	return out, nil
}
```

In `internal/db/user_limits.go`, replace the doc paragraph "Revoked keys are excluded — …" and the SQL's first subquery so the function reads:

```go
// Revoked keys are excluded — counting them would turn the ceiling into a
// lifetime quota. Suspended and expired keys DO count: both are one call
// away from carrying traffic again. Exchange keys (issued_via='exchange') do
// not count: KeyGrants bound them, one per granted tuple, and only platform
// admins write grants.
func UserKeyAllowance(ctx context.Context, pool *pgxpool.Pool, email string, defaultMax int) (used int, max int, err error) {
	const sql = `
		SELECT
		    (SELECT count(*) FROM environment_keys
		      WHERE lower(owner_email) = $1 AND status <> 'revoked' AND issued_via <> 'exchange'),
		    COALESCE((SELECT max_keys FROM user_limits WHERE email = $1), $2)
	`
```

(rest of the function unchanged).

- [ ] **Step 6: Write the grant rule**

`internal/keygrant/keygrant.go`:

```go
// SPDX-License-Identifier: Apache-2.0

// Package keygrant is the one rule deciding whether KeyGrants allow an
// exchange tuple (identity, Environment, "<namespace>/<serviceaccount>"): a
// single grant must name all three; lists from different grants are never
// combined. The exchange handler, the exchange-JWT resolver and the operator's
// cleanup sweep all decide through it.
package keygrant

import (
	"slices"

	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/litellm"
)

// Covering returns the grants that allow the tuple.
func Covering(grants []db.KeyGrantRow, identity, env, requester string) []db.KeyGrantRow {
	var out []db.KeyGrantRow
	for _, g := range grants {
		if g.Identity == identity && slices.Contains(g.Environments, env) && slices.Contains(g.ServiceAccounts, requester) {
			out = append(out, g)
		}
	}
	return out
}

// Miss names, for the audit record only, why grants already filtered to one
// identity allow nothing for env. The HTTP answer is always not_granted.
func Miss(grants []db.KeyGrantRow, env string) string {
	if len(grants) == 0 {
		return "no_grant_for_identity"
	}
	for _, g := range grants {
		if slices.Contains(g.Environments, env) {
			return "requester_not_granted"
		}
	}
	return "environment_not_granted"
}

// Budget is the lowest maxBudget among covering grants (first by name on a
// tie); nil when none sets one.
func Budget(covering []db.KeyGrantRow) *litellm.TagBudget {
	var best *litellm.TagBudget
	for _, g := range covering {
		if g.MaxBudget == nil {
			continue
		}
		if best == nil || *g.MaxBudget < best.MaxBudget {
			best = &litellm.TagBudget{MaxBudget: *g.MaxBudget, BudgetDuration: g.BudgetDuration}
		}
	}
	return best
}

// Plan splits non-revoked exchange keys into those no grant allows any more
// (revoke) and the ceiling each still-covered key carries (budgets, by key id;
// a key whose covering grants set no budget is absent).
func Plan(grants []db.KeyGrantRow, keys []db.EkKeyInfo) (revoke []db.EkKeyInfo, budgets map[string]litellm.TagBudget) {
	budgets = map[string]litellm.TagBudget{}
	for _, k := range keys {
		covering := Covering(grants, k.OwnerEmail, k.Environment, k.Requester)
		if len(covering) == 0 {
			revoke = append(revoke, k)
			continue
		}
		if b := Budget(covering); b != nil {
			budgets[k.KeyID] = *b
		}
	}
	return revoke, budgets
}
```

- [ ] **Step 7: Run the unit test to verify it passes**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/keygrant > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: `ok  github.com/ackstorm/ach/internal/keygrant`, `EXIT=0`.

- [ ] **Step 8: Write the integration test**

`internal/db/key_exchange_test.go`:

```go
//go:build integration

// SPDX-License-Identifier: Apache-2.0

// Integration tests for the exchange data model (migration 000026).

package db_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/ackstorm/ach/internal/db"
)

const exOwner = "ai.run@ackstorm.com"

func exchangeRow(keyID, hash, requester string) db.EkInsertRow {
	r := requester
	return db.EkInsertRow{KeyID: keyID, CredentialHash: hash, Environment: "prod", OwnerEmail: exOwner,
		Name: "exchange:" + requester, IssuedVia: "exchange", Requester: &r}
}

func TestKeyGrants_UpsertListDelete(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	pool, cleanup := setupPostgresForPhase2(t, ctx)
	defer cleanup()

	budget := 50.0
	g := db.KeyGrantRow{Namespace: "ach", Name: "ai-run-doctor", Identity: exOwner,
		Environments: []string{"k8s-doctor"}, ServiceAccounts: []string{"agents/agent-k8s-doctor"},
		MaxBudget: &budget, BudgetDuration: "30d"}
	if err := db.UpsertKeyGrant(ctx, pool, g); err != nil {
		t.Fatal(err)
	}
	g.Environments = []string{"k8s-doctor", "dev"}
	if err := db.UpsertKeyGrant(ctx, pool, g); err != nil {
		t.Fatal(err)
	}
	if err := db.UpsertKeyGrant(ctx, pool, db.KeyGrantRow{Namespace: "ach", Name: "other", Identity: "x@ackstorm.com",
		Environments: []string{"dev"}, ServiceAccounts: []string{"agents/x"}}); err != nil {
		t.Fatal(err)
	}

	if all, err := db.ListKeyGrants(ctx, pool, "ach"); err != nil || len(all) != 2 {
		t.Fatalf("ListKeyGrants = %v, %v", all, err)
	}
	mine, err := db.ListKeyGrantsForIdentity(ctx, pool, "ach", exOwner)
	if err != nil || len(mine) != 1 {
		t.Fatalf("ListKeyGrantsForIdentity = %v, %v", mine, err)
	}
	if got := mine[0]; len(got.Environments) != 2 || got.MaxBudget == nil || *got.MaxBudget != 50 || got.BudgetDuration != "30d" {
		t.Fatalf("upsert did not replace the row: %+v", got)
	}
	if other, _ := db.ListKeyGrantsForIdentity(ctx, pool, "elsewhere", exOwner); len(other) != 0 {
		t.Fatal("namespace not honoured")
	}
	if err := db.DeleteKeyGrant(ctx, pool, "ach", "ai-run-doctor"); err != nil {
		t.Fatal(err)
	}
	if err := db.DeleteKeyGrant(ctx, pool, "ach", "ai-run-doctor"); err != nil {
		t.Fatalf("deleting an absent row must be a no-op: %v", err)
	}
	if left, _ := db.ListKeyGrantsForIdentity(ctx, pool, "ach", exOwner); len(left) != 0 {
		t.Fatal("row survived delete")
	}
}

func TestExchangeKeys_OneLiveKeyPerTuple(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	pool, cleanup := setupPostgresForPhase2(t, ctx)
	defer cleanup()

	mustInsertEk(t, pool, exchangeRow("ekid_a", "hash-a", "agents/doctor"))
	err := db.InsertEnvironmentKey(ctx, pool, exchangeRow("ekid_b", "hash-b", "agents/doctor"))
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || pgErr.ConstraintName != db.ExchangeTupleConstraint {
		t.Fatalf("second live key for one tuple: want %s violation, got %v", db.ExchangeTupleConstraint, err)
	}
	mustInsertEk(t, pool, exchangeRow("ekid_c", "hash-c", "agents/other")) // another requester, another tuple

	if _, err := db.RevokeEnvironmentKey(ctx, pool, "ekid_a"); err != nil {
		t.Fatal(err)
	}
	mustInsertEk(t, pool, exchangeRow("ekid_b", "hash-b", "agents/doctor")) // a revoked key frees its tuple

	got, err := db.FindExchangeKey(ctx, pool, exOwner, "prod", "agents/doctor")
	if err != nil || got == nil || got.KeyID != "ekid_b" || got.Requester != "agents/doctor" {
		t.Fatalf("FindExchangeKey = %+v, %v", got, err)
	}
	live, err := db.ListExchangeKeys(ctx, pool)
	if err != nil || len(live) != 2 || live[0].KeyID != "ekid_b" || live[1].KeyID != "ekid_c" {
		t.Fatalf("ListExchangeKeys = %+v, %v", live, err)
	}
}

func TestExchangeKeyResolve_OnlyActiveExchangeKeys(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	pool, cleanup := setupPostgresForPhase2(t, ctx)
	defer cleanup()

	mustInsertEk(t, pool, db.EkInsertRow{KeyID: "ekid_manual", CredentialHash: "hash-m",
		Environment: "prod", OwnerEmail: exOwner, Name: "laptop"})
	mustInsertEk(t, pool, exchangeRow("ekid_ex", "hash-x", "agents/doctor"))

	if r, err := db.ExchangeKeyResolve(ctx, pool, "ekid_manual"); err != nil || r != nil {
		t.Fatalf("a manual key must never resolve as an exchange key: %+v, %v", r, err)
	}
	r, err := db.ExchangeKeyResolve(ctx, pool, "ekid_ex")
	if err != nil || r == nil || r.Requester != "agents/doctor" || r.OwnerEmail != exOwner || r.Environment != "prod" {
		t.Fatalf("ExchangeKeyResolve = %+v, %v", r, err)
	}
	if _, err := db.SuspendEnvironmentKey(ctx, pool, "ekid_ex"); err != nil {
		t.Fatal(err)
	}
	if r, _ := db.ExchangeKeyResolve(ctx, pool, "ekid_ex"); r != nil {
		t.Fatal("a suspended exchange key resolved")
	}
	if _, err := db.RevokeEnvironmentKey(ctx, pool, "ekid_ex"); err != nil {
		t.Fatal(err)
	}
	if r, _ := db.ExchangeKeyResolve(ctx, pool, "ekid_ex"); r != nil {
		t.Fatal("a revoked exchange key resolved")
	}
}

func TestUserKeyAllowance_ExcludesExchangeKeys(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	pool, cleanup := setupPostgresForPhase2(t, ctx)
	defer cleanup()

	mustInsertEk(t, pool, db.EkInsertRow{KeyID: "ekid_manual", CredentialHash: "hash-m",
		Environment: "prod", OwnerEmail: exOwner, Name: "laptop"})
	mustInsertEk(t, pool, exchangeRow("ekid_ex", "hash-x", "agents/doctor"))
	used, _, err := db.UserKeyAllowance(ctx, pool, exOwner, 5)
	if err != nil || used != 1 {
		t.Fatalf("used = %d, %v; want 1 (the exchange key does not count)", used, err)
	}
}

func TestInsertEnvironmentKey_IssuedViaDefaultsAndCheck(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	pool, cleanup := setupPostgresForPhase2(t, ctx)
	defer cleanup()

	mustInsertEk(t, pool, db.EkInsertRow{KeyID: "ekid_manual", CredentialHash: "hash-m",
		Environment: "prod", OwnerEmail: exOwner, Name: "laptop"})
	var via string
	var requester *string
	if err := pool.QueryRow(ctx, `SELECT issued_via, requester FROM environment_keys WHERE key_id = 'ekid_manual'`).
		Scan(&via, &requester); err != nil {
		t.Fatal(err)
	}
	if via != "manual" || requester != nil {
		t.Fatalf("issued_via=%q requester=%v; want manual/NULL", via, requester)
	}
	r := "agents/x"
	if err := db.InsertEnvironmentKey(ctx, pool, db.EkInsertRow{KeyID: "ekid_bad", CredentialHash: "hash-bad",
		Environment: "prod", OwnerEmail: exOwner, Name: "bad", Requester: &r}); err == nil {
		t.Fatal("a manual key with a requester must be refused by environment_keys_requester_chk")
	}
}
```

- [ ] **Step 9: Run the integration tests**

Run: `LOG=$(mktemp); timeout 1200 make test-integration-pkg PKG=./internal/db FOCUS='KeyGrants|ExchangeKey|UserKeyAllowance|InsertEnvironmentKey|EnvironmentKey' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- (FAIL|SKIP)|^(ok|FAIL)' "$LOG"`
Expected: `ok  github.com/ackstorm/ach/internal/db`, `EXIT=0`, no `--- SKIP` (a SKIP means Docker was unavailable — fix that, do not accept it). The pre-existing environment-key tests in the focus prove the insert change kept them green.

- [ ] **Step 10: Commit**

```bash
git add db/migrations/000026_key_exchange.up.sql db/migrations/000026_key_exchange.down.sql \
  internal/db/key_grants.go internal/db/environment_keys.go internal/db/types_keys.go \
  internal/db/user_limits.go internal/db/key_exchange_test.go internal/keygrant/
git commit -m "feat(db): exchange key columns, key_grants table and the grant rule"
```

---

### Task 2: Resolve exchange JWTs in forwarder, content service and platform API

**Files:**
- Modify: `internal/keys/keys.go:198-205` (add `PeekAudience` after `LooksLikeJWS`); Create: `internal/keys/peek_test.go`
- Modify: `internal/forwarder/jwt/verify.go` (whole file), `internal/forwarder/jwt/signer.go:36-65,178-206` (`Claims.JTI`); Create: `internal/forwarder/jwt/exchange_claims_test.go`
- Create: `internal/keystore/exchangeresolver.go`, `internal/keystore/exchangeresolver_test.go`
- Modify: `internal/keystore/dbresolver.go:191-214` (`ekLookupFor` uses new `keyInfoFromEK`)
- Modify: `cmd/ach/cmd/forwarder.go:349`, `cmd/ach/cmd/content_service.go:192`, `cmd/ach/cmd/platform_api.go:493`
- Modify: `docs/developer-guide/jwt-forwarder.md` (after the trusted-IdP failure table, ~line 380)

**Interfaces:**
- Consumes: `db.ExchangeKeyResolve`, `db.ListKeyGrantsForIdentity`, `db.EkKeyInfo.Requester`, `keygrant.Covering` (Task 1).
- Produces:
  - `keys.PeekAudience(raw string) []string`
  - `jwt.Claims.JTI string` (emitted as `jti` only when non-empty); `(*jwt.Ed25519Signer).VerifyExp(raw, iss, aud string) (sub string, exp time.Time, err error)`
  - `keystore.ExchangeAudience = "ach-ek"`
  - `keystore.ExchangeVerifier` interface `{ VerifyExp(raw, iss, aud string) (string, time.Time, error) }`
  - `keystore.ExchangeKeyLookup func(ctx context.Context, keyID string) (*db.EkKeyInfo, error)`
  - `keystore.NewExchangeResolver(inner Resolver, v ExchangeVerifier, iss string, lookup ExchangeKeyLookup) Resolver`
  - `keystore.NewExchangeResolverDB(inner Resolver, v ExchangeVerifier, iss string, pool *pgxpool.Pool, ns string) Resolver`

- [ ] **Step 1: Write the failing tests**

`internal/keys/peek_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package keys

import (
	"encoding/base64"
	"slices"
	"testing"
)

func TestPeekAudience(t *testing.T) {
	enc := func(payload string) string {
		return "e30." + base64.RawURLEncoding.EncodeToString([]byte(payload)) + ".c2ln"
	}
	cases := map[string]struct {
		raw  string
		want []string
	}{
		"string":     {enc(`{"aud":"https://ach.test"}`), []string{"https://ach.test"}},
		"list":       {enc(`{"aud":["a","https://ach.test"]}`), []string{"a", "https://ach.test"}},
		"absent":     {enc(`{"sub":"x"}`), nil},
		"not base64": {"a.%%%.c", nil},
		"not a JWS":  {"ek-abc", nil},
	}
	for name, tc := range cases {
		if got := PeekAudience(tc.raw); !slices.Equal(got, tc.want) {
			t.Errorf("%s: got %v want %v", name, got, tc.want)
		}
	}
}
```

`internal/forwarder/jwt/exchange_claims_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package jwt

import (
	"context"
	"testing"
	"time"

	jwtv5 "github.com/golang-jwt/jwt/v5"
)

func TestSignJTIAndVerifyExp(t *testing.T) {
	s := NewEd25519Signer()
	if err := LoadSeed(s, "k1", make([]byte, 32)); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	raw, err := s.Sign(ctx, Claims{Iss: "https://ach.test", Sub: "ekid_1", Aud: "ach-ek", TTL: 15 * time.Minute, JTI: "j-1"})
	if err != nil {
		t.Fatal(err)
	}
	claims := jwtv5.MapClaims{}
	if _, _, err := jwtv5.NewParser().ParseUnverified(raw, claims); err != nil {
		t.Fatal(err)
	}
	if claims["jti"] != "j-1" {
		t.Fatalf("jti = %v", claims["jti"])
	}
	sub, exp, err := s.VerifyExp(raw, "https://ach.test", "ach-ek")
	if err != nil || sub != "ekid_1" {
		t.Fatalf("VerifyExp = %q, %v", sub, err)
	}
	if d := time.Until(exp); d < 14*time.Minute || d > 15*time.Minute+time.Second {
		t.Fatalf("exp in %v, want ~15m", d)
	}
	if _, _, err := s.VerifyExp(raw, "https://ach.test", "ach"); err == nil {
		t.Fatal("an ach-ek token verified for audience ach")
	}

	plain, err := s.Sign(ctx, Claims{Iss: "i", Sub: "s", Aud: "a"})
	if err != nil {
		t.Fatal(err)
	}
	c2 := jwtv5.MapClaims{}
	if _, _, err := jwtv5.NewParser().ParseUnverified(plain, c2); err != nil {
		t.Fatal(err)
	}
	if _, ok := c2["jti"]; ok {
		t.Fatal("jti emitted without Claims.JTI")
	}
}
```

`internal/keystore/exchangeresolver_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package keystore

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/forwarder/jwt"
	"github.com/ackstorm/ach/internal/keys"
)

func liveExchangeRow() *db.EkKeyInfo {
	tok, mat := "lt-ek", "sealed-ek"
	return &db.EkKeyInfo{KeyID: "ekid_1", Environment: "prod", OwnerEmail: "ai.run@ackstorm.com",
		LiteLLMToken: &tok, LiteLLMKeyMaterial: &mat, Status: "active", Requester: "agents/doctor"}
}

func exchangeLookup(rows map[string]*db.EkKeyInfo) ExchangeKeyLookup {
	return func(_ context.Context, keyID string) (*db.EkKeyInfo, error) { return rows[keyID], nil }
}

func TestExchangeResolver(t *testing.T) {
	s := oauthSigner(t)
	ctx := context.Background()
	r := NewExchangeResolver(fallthroughResolver(), s, "https://ach.test/",
		exchangeLookup(map[string]*db.EkKeyInfo{"ekid_1": liveExchangeRow()}))
	sign := func(signer *jwt.Ed25519Signer, c jwt.Claims) string {
		raw, err := signer.Sign(ctx, c)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	claims := func(iss, sub string, ttl time.Duration) jwt.Claims {
		return jwt.Claims{Iss: iss, Sub: sub, Aud: ExchangeAudience, TTL: ttl}
	}

	info, err := r.Resolve(ctx, sign(s, claims("https://ach.test", "ekid_1", time.Minute)))
	if err != nil || info == nil {
		t.Fatalf("info=%v err=%v", info, err)
	}
	if info.KeyType != keys.PrefixEk || info.KeyID != "ekid_1" || info.Environment != "prod" ||
		info.OwnerEmail != "ai.run@ackstorm.com" || info.LiteLLMKeyMaterial == nil {
		t.Fatalf("%+v", info)
	}
	if info.ExpiresAt == nil || info.ExpiresAt.After(time.Now().Add(time.Minute+time.Second)) {
		t.Fatalf("a cache entry must not outlive the JWT: ExpiresAt=%v", info.ExpiresAt)
	}

	other := jwt.NewEd25519Signer()
	seed := make([]byte, 32)
	for i := range seed {
		seed[i] = byte(100 + i)
	}
	if err := jwt.LoadSeed(other, "k2", seed); err != nil {
		t.Fatal(err)
	}
	for name, raw := range map[string]string{
		"expired":           sign(s, claims("https://ach.test", "ekid_1", -time.Minute)),
		"unknown kid":       sign(other, claims("https://ach.test", "ekid_1", time.Minute)),
		"wrong issuer":      sign(s, claims("https://other.test", "ekid_1", time.Minute)),
		"manual or gone id": sign(s, claims("https://ach.test", "ekid_manual", time.Minute)),
	} {
		if info, err := r.Resolve(ctx, raw); err != nil || info != nil {
			t.Errorf("%s: info=%v err=%v; want (nil, nil) = 401", name, info, err)
		}
	}

	down := NewExchangeResolver(fallthroughResolver(), s, "https://ach.test",
		func(context.Context, string) (*db.EkKeyInfo, error) { return nil, errors.New("db down") })
	if _, err := down.Resolve(ctx, sign(s, claims("https://ach.test", "ekid_1", time.Minute))); err == nil {
		t.Fatal("a lookup error must surface (500), not read as 401")
	}
}

// TestExchangeAndOAuthResolversInProductionOrder wires the two resolvers the
// way every service does — cache(exchange(oauth(db))) — and checks each
// refuses the other's token.
func TestExchangeAndOAuthResolversInProductionOrder(t *testing.T) {
	s := oauthSigner(t)
	ctx := context.Background()
	pk := func(_ context.Context, email string) (*db.PkKeyInfo, error) {
		if email != "u@x.com" {
			return nil, nil
		}
		return &db.PkKeyInfo{KeyID: "pkid_1", OwnerEmail: email, ExpiresAt: time.Now().Add(time.Hour), Status: "active"}, nil
	}
	ekLookups := 0
	ek := func(_ context.Context, keyID string) (*db.EkKeyInfo, error) {
		ekLookups++
		if keyID == "ekid_1" {
			return liveExchangeRow(), nil
		}
		return nil, nil
	}
	oauth := NewOAuthResolver(fallthroughResolver(), s, "https://ach.test", "ach", pk)
	chain := NewExchangeResolver(oauth, s, "https://ach.test", ek)

	exTok, _ := s.Sign(ctx, jwt.Claims{Iss: "https://ach.test", Sub: "ekid_1", Aud: ExchangeAudience, TTL: time.Minute})
	oaTok, _ := s.Sign(ctx, jwt.Claims{Iss: "https://ach.test", Sub: "u@x.com", Aud: "ach", TTL: time.Minute})

	if info, err := chain.Resolve(ctx, exTok); err != nil || info == nil || info.KeyType != keys.PrefixEk {
		t.Fatalf("chain, exchange JWT: info=%v err=%v", info, err)
	}
	if info, err := chain.Resolve(ctx, oaTok); err != nil || info == nil || info.KeyType != keys.PrefixPk {
		t.Fatalf("chain, OAuth JWT: info=%v err=%v", info, err)
	}
	if info, _ := oauth.Resolve(ctx, exTok); info != nil {
		t.Fatal("the OAuth resolver accepted an exchange JWT")
	}
	before := ekLookups
	if info, _ := NewExchangeResolver(fallthroughResolver(), s, "https://ach.test", ek).Resolve(ctx, oaTok); info != nil {
		t.Fatal("the exchange resolver accepted an OAuth JWT")
	}
	if ekLookups != before {
		t.Fatal("an OAuth JWT reached the exchange-key lookup")
	}
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `LOG=$(mktemp); timeout 900 make test-unit-pkg PKG='./internal/keys ./internal/forwarder/jwt ./internal/keystore' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|undefined' "$LOG"`
Expected: FAIL — `undefined: PeekAudience`, `unknown field JTI`, `s.VerifyExp undefined`, `undefined: NewExchangeResolver`.

- [ ] **Step 3: Implement `PeekAudience`**

Append to `internal/keys/keys.go` (add `"encoding/json"` to its imports):

```go
// PeekAudience returns a compact JWS's aud claim (a string or a list) WITHOUT
// verifying it — routing only, never trust. nil when absent or unreadable.
func PeekAudience(raw string) []string {
	parts := strings.Split(raw, ".")
	if len(parts) != 3 {
		return nil
	}
	b, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil
	}
	var c struct {
		Aud json.RawMessage `json:"aud"`
	}
	if json.Unmarshal(b, &c) != nil {
		return nil
	}
	var one string
	if json.Unmarshal(c.Aud, &one) == nil {
		return []string{one}
	}
	var many []string
	if json.Unmarshal(c.Aud, &many) == nil {
		return many
	}
	return nil
}
```

- [ ] **Step 4: Implement `jti` and `VerifyExp`**

In `internal/forwarder/jwt/signer.go`, change the `Claims` doc sentence "NO jti claim is emitted (Hub §9.1 + §20, accepted v1alpha1 threat model — replay-window restatement is v1beta1 backlog)." to "A jti claim is emitted only when JTI is set (exchange tokens); backend tokens carry none." and add the field after `TTL`:

```go
	// JTI is the "jti" claim; omitted when empty. Exchange tokens
	// (POST /platform/keys/exchange) carry one; backend tokens do not.
	JTI string
```

In `Sign`, after the `groups` block:

```go
	if c.JTI != "" {
		claims["jti"] = c.JTI
	}
```

Replace `internal/forwarder/jwt/verify.go` with:

```go
// SPDX-License-Identifier: Apache-2.0

package jwt

import (
	"errors"
	"fmt"
	"strings"
	"time"

	jwtv5 "github.com/golang-jwt/jwt/v5"
)

// ErrNotAJWT is returned for input that is not three dot-separated
// segments. Callers use it to fall through to pk_/ek_ handling.
var ErrNotAJWT = errors.New("jwt: not a compact JWS")

// Verify checks a token this signer (or its next slot) produced and returns
// its subject. iss and aud are exact matches. Keys are the
// in-memory slots — no JWKS fetch, both services hold the same Secret.
func (s *Ed25519Signer) Verify(raw, iss, aud string) (string, error) {
	sub, _, err := s.VerifyExp(raw, iss, aud)
	return sub, err
}

// VerifyExp is Verify that also returns the token's expiry, so a cached
// resolution never outlives the token.
func (s *Ed25519Signer) VerifyExp(raw, iss, aud string) (string, time.Time, error) {
	if strings.Count(raw, ".") != 2 {
		return "", time.Time{}, ErrNotAJWT
	}
	keyfunc := func(t *jwtv5.Token) (any, error) {
		kid, _ := t.Header["kid"].(string)
		for _, slot := range []*signerSlot{s.current.Load(), s.next.Load()} {
			if slot != nil && slot.kid == kid {
				return slot.pub, nil
			}
		}
		return nil, fmt.Errorf("unknown kid %q", kid)
	}
	tok, err := jwtv5.Parse(raw, keyfunc,
		jwtv5.WithValidMethods([]string{jwtv5.SigningMethodEdDSA.Alg()}),
		jwtv5.WithExpirationRequired(),
		jwtv5.WithIssuer(iss),
		jwtv5.WithAudience(aud),
	)
	if err != nil {
		return "", time.Time{}, err
	}
	sub, err := tok.Claims.GetSubject()
	if err != nil || sub == "" {
		return "", time.Time{}, errors.New("jwt: no subject")
	}
	exp, err := tok.Claims.GetExpirationTime()
	if err != nil || exp == nil {
		return "", time.Time{}, errors.New("jwt: no exp")
	}
	return sub, exp.Time, nil
}
```

- [ ] **Step 5: Implement the exchange resolver**

In `internal/keystore/dbresolver.go`, replace the `return &KeyInfo{…}, nil` literal at the end of `ekLookupFor` with `return keyInfoFromEK(row), nil` and add:

```go
// keyInfoFromEK maps a resolved environment_keys row to the auth-layer view.
// Shared by the ek_ path and the exchange-JWT resolver (same row shape).
func keyInfoFromEK(row *db.EkKeyInfo) *KeyInfo {
	return &KeyInfo{
		KeyID:         row.KeyID,
		KeyType:       keys.PrefixEk,
		OwnerEmail:    row.OwnerEmail,
		ExpiresAt:     row.ExpiresAt,
		Environment:   row.Environment,
		LiteLLMUserID: row.LiteLLMUserID,
		LiteLLMToken:  row.LiteLLMToken,
		// TESTING-PHASE (reverts FIX01 §A.6)
		LiteLLMKeyMaterial: row.LiteLLMKeyMaterial,
	}
}
```

`internal/keystore/exchangeresolver.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package keystore

import (
	"context"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/keygrant"
	"github.com/ackstorm/ach/internal/keys"
)

// ExchangeAudience is the aud of a JWT minted by POST /platform/keys/exchange.
// It differs from the OAuth audience "ach", so neither resolver accepts the
// other's token.
const ExchangeAudience = "ach-ek"

// ExchangeVerifier is what *jwt.Ed25519Signer provides for exchange JWTs.
type ExchangeVerifier interface {
	VerifyExp(raw, iss, aud string) (sub string, exp time.Time, err error)
}

// ExchangeKeyLookup returns the live exchange key a JWT subject names while a
// KeyGrant still allows its tuple, or (nil, nil).
type ExchangeKeyLookup func(ctx context.Context, keyID string) (*db.EkKeyInfo, error)

type exchangeResolver struct {
	inner  Resolver
	v      ExchangeVerifier
	iss    string
	lookup ExchangeKeyLookup
}

// NewExchangeResolver wraps the OAuth resolver. Order is load-bearing: the
// OAuth resolver answers 401 for any JWS that fails its audience, so a JWS
// whose unverified aud names ExchangeAudience is decided HERE (verified, then
// mapped to its exchange key — the same KeyInfo an ek_ yields), and every
// other bearer goes to inner unchanged. A JWS with that audience that fails
// verification, or names no live granted key, is (nil, nil) — 401. Sits
// inside the Redis cache: the 60-second window and singleflight apply, and
// the cached entry expires with the JWT.
func NewExchangeResolver(inner Resolver, v ExchangeVerifier, iss string, lookup ExchangeKeyLookup) Resolver {
	return &exchangeResolver{inner: inner, v: v, iss: strings.TrimRight(iss, "/"), lookup: lookup}
}

// NewExchangeResolverDB is the production constructor: the key row must be an
// active exchange key AND a KeyGrant in ns must still allow its tuple, so
// removing a grant entry cuts access within the cache window.
func NewExchangeResolverDB(inner Resolver, v ExchangeVerifier, iss string, pool *pgxpool.Pool, ns string) Resolver {
	return NewExchangeResolver(inner, v, iss, func(ctx context.Context, keyID string) (*db.EkKeyInfo, error) {
		row, err := db.ExchangeKeyResolve(ctx, pool, keyID)
		if err != nil || row == nil {
			return nil, err
		}
		grants, err := db.ListKeyGrantsForIdentity(ctx, pool, ns, row.OwnerEmail)
		if err != nil {
			return nil, err
		}
		if len(keygrant.Covering(grants, row.OwnerEmail, row.Environment, row.Requester)) == 0 {
			return nil, nil
		}
		return row, nil
	})
}

func (r *exchangeResolver) Resolve(ctx context.Context, plaintext string) (*KeyInfo, error) {
	if !keys.LooksLikeJWS(plaintext) || !slices.Contains(keys.PeekAudience(plaintext), ExchangeAudience) {
		return r.inner.Resolve(ctx, plaintext)
	}
	keyID, exp, err := r.v.VerifyExp(plaintext, r.iss, ExchangeAudience)
	if err != nil {
		return nil, nil
	}
	row, err := r.lookup(ctx, keyID)
	if err != nil || row == nil {
		return nil, err
	}
	info := keyInfoFromEK(row)
	if info.ExpiresAt == nil || exp.Before(*info.ExpiresAt) {
		info.ExpiresAt = &exp
	}
	return info, nil
}
```

- [ ] **Step 6: Wire it in the three services**

`cmd/ach/cmd/forwarder.go` — after the `oauthResolver := keystore.NewOAuthResolverDB(dbResolver, out.signer, cfg.BaseURL, "ach", pool)` line:

```go
	// Exchange JWTs (aud ach-ek) resolve to their exchange key while a
	// KeyGrant still allows it; every other bearer goes on to the OAuth resolver.
	exchangeResolver := keystore.NewExchangeResolverDB(oauthResolver, out.signer, cfg.BaseURL, pool, cfg.Namespace)
```

and change `keystore.NewCachedResolver(oauthResolver, out.redis, …` to `keystore.NewCachedResolver(exchangeResolver, out.redis, …`.

`cmd/ach/cmd/content_service.go` — after `oauthResolver := keystore.NewOAuthResolverDB(dbResolver, verifier, cfg.BaseURL, "ach", pool)`:

```go
	exchangeResolver := keystore.NewExchangeResolverDB(oauthResolver, verifier, cfg.BaseURL, pool, cfg.Namespace)
```

and change `keystore.NewCachedResolver(oauthResolver, redisClient, cfg.Pepper)` to `keystore.NewCachedResolver(exchangeResolver, redisClient, cfg.Pepper)`.

`cmd/ach/cmd/platform_api.go` — after `oauthResolver := keystore.NewOAuthResolverDB(dbResolver, signer, cfg.BaseURL, "ach", pool)`:

```go
	exchangeResolver := keystore.NewExchangeResolverDB(oauthResolver, signer, cfg.BaseURL, pool, cfg.Namespace)
```

and change `keystore.NewCachedResolver(oauthResolver, out.redis, cfg.Pepper,` to `keystore.NewCachedResolver(exchangeResolver, out.redis, cfg.Pepper,`.

No forwarder/content-service Authn change is needed: a JWS in `x-ach-key` (declared `resolve` slot) or in `Authorization: Bearer` is already handed to the resolver (`internal/platformapi/middleware/middleware.go` `credential`, `internal/contentservice/authz.go:92`).

- [ ] **Step 7: Document the accepted credential**

In `docs/developer-guide/jwt-forwarder.md`, after the trusted-IdP failure table (the row beginning `| token invalid (sig, alg, exp, aud, claim,`), add:

```markdown
### Exchange JWTs (`aud: ach-ek`)

`POST /platform/keys/exchange` (platform API) signs a JWT with the same
Ed25519 slots: `iss` = `ACH_BASE_URL`, `aud` = `ach-ek`, `sub` = an exchange
key id (`ekid_…`), `jti`, lifetime `ACH_KEY_EXCHANGE_TOKEN_TTL` (15 minutes).
`keystore.NewExchangeResolver` wraps the OAuth resolver inside the Redis cache
in the forwarder, content service and platform API: a JWS whose unverified
`aud` is `ach-ek` is verified there and resolves to the exchange key — the same
`KeyInfo` an `ek_` yields, so `EkOwnerGate`, the `key:<id>` budget tag and
LiteLLM routing are unchanged — only while a `KeyGrant` still allows its
(identity, Environment, ServiceAccount) tuple. Any other JWS goes to the OAuth
resolver (`aud: ach`). It is accepted in `x-ach-key` and in
`Authorization: Bearer`. The cache entry expires with the JWT.
```

- [ ] **Step 8: Run the tests and a build**

Run: `LOG=$(mktemp); timeout 900 make test-unit-pkg PKG='./internal/keys ./internal/forwarder/jwt ./internal/keystore' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: three `ok` lines, `EXIT=0`.

Run: `LOG=$(mktemp); timeout 900 ./scripts/dev.sh go build ./... > "$LOG" 2>&1; echo "EXIT=$?"; cat "$LOG"`
Expected: `EXIT=0`, empty output.

- [ ] **Step 9: Commit**

```bash
git add internal/keys/keys.go internal/keys/peek_test.go internal/forwarder/jwt/ \
  internal/keystore/exchangeresolver.go internal/keystore/exchangeresolver_test.go internal/keystore/dbresolver.go \
  cmd/ach/cmd/forwarder.go cmd/ach/cmd/content_service.go cmd/ach/cmd/platform_api.go \
  docs/developer-guide/jwt-forwarder.md
git commit -m "feat(keystore): resolve exchange JWTs to their granted exchange key"
```

---

### Task 3: Factor the ek_ mint out of `mintAndInsert`

Pure refactor: `POST /platform/keys` behaves exactly as before. `mint` inserts and returns the key; each caller writes its own response and success audit, and every event the shared steps emit carries the caller's audit action.

**Files:**
- Modify: `internal/platformapi/envkeys/handler.go:271-293` (`CreateHandler` tail), `:301-311` (`createReq`), `:313-416` (shared steps), `:471-688` (`mintAndInsert` → `mint`)
- Create: `internal/platformapi/envkeys/mint_test.go`

**Interfaces:**
- Produces (package `envkeys`, unexported, used by Task 4):
  - `createReq` fields `action string` (audit action of every event), `extra map[string]string` (audit `Extra` of every event)
  - `func (cr *createReq) mint(env *db.EnvironmentRow, userID string) (keyID, plaintext string, ok bool)` — on failure it has written the response and audit; on success it has written nothing
  - test helper `prodEnv() *db.EnvironmentRow` (in `mint_test.go`)

- [ ] **Step 1: Write the failing test**

`internal/platformapi/envkeys/mint_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package envkeys

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ackstorm/ach/internal/audit"
	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/litellm"
	"github.com/ackstorm/ach/internal/platformapi/middleware"
)

func prodEnv() *db.EnvironmentRow {
	return &db.EnvironmentRow{Namespace: "ach", Name: "prod", AuthorizedTeams: []string{"default"}}
}

func newMintReq(deps Deps, rec *httptest.ResponseRecorder, action string) *createReq {
	return &createReq{
		deps: deps, w: rec, ctx: context.Background(), action: action, extra: map[string]string{"k": "v"},
		req:    CreateRequest{Environment: "prod", Name: "k"},
		keyCtx: middleware.KeyContext{OwnerEmail: "user@example.com"},
		target: &audit.Target{Kind: "environment", Name: "prod"},
	}
}

// TestMint_LeavesResponseAndAuditToCaller pins the seam POST /platform/keys
// and POST /platform/keys/exchange both build on.
func TestMint_LeavesResponseAndAuditToCaller(t *testing.T) {
	fdb := &fakeEkDB{}
	deps, _ := newCreateDeps(fdb)
	var auditBuf bytes.Buffer
	deps.Audit = slog.New(slog.NewJSONHandler(&auditBuf, nil))
	rec := httptest.NewRecorder()

	keyID, plaintext, ok := newMintReq(deps, rec, audit.ActionEkCreate).mint(prodEnv(), "llu-user@example.com")
	if !ok || !strings.HasPrefix(keyID, "ekid_") || !strings.HasPrefix(plaintext, "ek-") {
		t.Fatalf("mint = %q, %q, %v", keyID, plaintext, ok)
	}
	if fdb.inserted == nil || fdb.inserted.KeyID != keyID {
		t.Fatalf("inserted = %+v", fdb.inserted)
	}
	if rec.Body.Len() != 0 || auditBuf.Len() != 0 {
		t.Fatalf("mint wrote on success: body=%q audit=%q", rec.Body.String(), auditBuf.String())
	}
}

func TestMint_FailureAuditsWithCallerAction(t *testing.T) {
	deps, flm := newCreateDeps(&fakeEkDB{})
	flm.teams = []litellm.TeamListEntry{{TeamID: "team-uuid-default", TeamAlias: "default"}} // no ach-env-prod shell
	var auditBuf bytes.Buffer
	deps.Audit = slog.New(slog.NewJSONHandler(&auditBuf, nil))
	rec := httptest.NewRecorder()

	if _, _, ok := newMintReq(deps, rec, "platform.ek.test").mint(prodEnv(), "u"); ok {
		t.Fatal("mint succeeded without a shell team")
	}
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", rec.Code)
	}
	if !strings.Contains(auditBuf.String(), `"action":"platform.ek.test"`) || !strings.Contains(auditBuf.String(), `"k":"v"`) {
		t.Fatalf("audit does not carry the caller's action and extra: %s", auditBuf.String())
	}
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/platformapi/envkeys > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|unknown field|undefined' "$LOG"`
Expected: FAIL — `unknown field action in struct literal`, `cr.mint undefined`.

- [ ] **Step 3: Carry the audit action on `createReq`**

Add to the `createReq` struct, after `target *audit.Target`:

```go
	// action is the audit action every event of this request carries
	// (platform.ek.create, platform.ek.exchange); extra rides on each event.
	action string
	extra  map[string]string
```

In `CreateHandler`, add `action: audit.ActionEkCreate,` to the `cr := &createReq{…}` literal.

- [ ] **Step 4: Turn `mintAndInsert` into `mint`**

1. Rename and retype the method, and update its doc comment's last sentence:

```go
// mint runs §8.2 steps 7+8: server-side plaintext + key_id generation (D-13),
// the LiteLLM KeyGenerate with the Enterprise-tags drop-and-retry fallback,
// the INSERT with the WARN-03 ekid_-collision single retry + compensation
// RevokeKey under a fresh context, and the optional key:<id> budget. On any
// failure it writes the response + audit itself and returns ok=false; on
// success it writes nothing — the caller owns the response and its audit.
func (cr *createReq) mint(env *db.EnvironmentRow, userID string) (string, string, bool) {
```

2. Inside the body, every bare `return` becomes `return "", "", false` (nine sites: NewBearer, NewKeyID, credhash, shell-team lookup error, empty shell team id, KeyGenerate, seal, insert failure, budget failure).
3. Delete the whole `// Step 8 success: audit + respond.` block (the `audit.EmitAudit(… OutcomeCreated …)`, the `expiresAtResp` computation and the `render.JSON(…CreateResponse…)`) and end the method with:

```go
	return keyID, plaintext, true
}
```

4. Replace the tail of `CreateHandler` (`// Steps 7+8: mint + insert + success response.` and `cr.mintAndInsert(env, userID)`) with:

```go
		// Steps 7+8: mint + insert, then this route's own audit + response.
		keyID, plaintext, ok := cr.mint(env, userID)
		if !ok {
			return
		}
		audit.EmitAudit(ctx, deps.Audit, audit.Event{
			Action:    audit.ActionEkCreate,
			Outcome:   audit.OutcomeCreated,
			Actor:     actor,
			RequestID: reqID,
			KeyID:     keyID,
			Target:    cr.target,
		})
		var expiresAtResp *string
		if expiresAt != nil {
			s := expiresAt.Format(time.RFC3339)
			expiresAtResp = &s
		}
		render.JSON(w, http.StatusOK, CreateResponse{
			KeyID:       keyID,
			Plaintext:   plaintext,
			Environment: req.Environment,
			Name:        req.Name,
			OwnerEmail:  keyCtx.OwnerEmail,
			CreatedAt:   time.Now().UTC().Format(time.RFC3339),
			ExpiresAt:   expiresAtResp,
		})
```

5. Make every event emitted by the shared steps use the request's action. Between `func (cr *createReq) emitInternalError` and `func classifyInsertError` (and only there — `CreateHandler`'s own pre-`cr` events stay `ActionEkCreate`):

```bash
f=internal/platformapi/envkeys/handler.go
start=$(grep -n '^func (cr \*createReq) emitInternalError' "$f" | cut -d: -f1)
end=$(grep -n '^func classifyInsertError' "$f" | cut -d: -f1)
sed -i -E "${start},${end}s/Action:( +)audit\.ActionEkCreate,/Action:\1cr.action, Extra: cr.extra,/" "$f"
grep -c 'Action: *cr.action, Extra: cr.extra,' "$f"   # expect 8
timeout 300 ./scripts/dev.sh gofmt -w "$f"
```

Expected count: `8` (emitInternalError 1, emitLitellmError 1, validateAndLoadEnv 3, validateTeamMembership 1, mint 2).

- [ ] **Step 5: Run the package tests**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/platformapi/envkeys > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: `ok  github.com/ackstorm/ach/internal/platformapi/envkeys`, `EXIT=0` — the existing create/revoke/suspend tests pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add internal/platformapi/envkeys/handler.go internal/platformapi/envkeys/mint_test.go
git commit -m "refactor(envkeys): factor the ek_ mint out of the create response"
```

---

### Task 4: `POST /platform/keys/exchange` and its TokenReview binding

**Files:**
- Create: `internal/platformapi/envkeys/exchange.go`, `internal/platformapi/envkeys/exchange_test.go`
- Modify: `internal/platformapi/envkeys/handler.go` (`createReq` fields; `mint` metadata/insert/race; `classifyInsertError`)
- Modify: `internal/audit/events.go:49-60,70-116`, `internal/audit/events_test.go`
- Modify: `internal/platformapi/middleware/middleware.go:175,182-193` (`clientIP` → exported `ClientIP`)
- Modify: `internal/platformapi/adapters.go` (two methods), `internal/platformapi/server.go` (Deps fields; route), `internal/platformapi/server_test.go`
- Create: `cmd/ach/cmd/tokenreview.go`; Modify: `cmd/ach/cmd/platform_api.go` (config + wiring)
- Modify: `deploy/helm/ach/templates/platform-api-rbac.yaml`, `deploy/helm/ach/templates/platform-api-deployment.yaml:123-126`, `deploy/helm/ach/values.yaml:115-170` (platformApi block), `scripts/helm-render-check.sh`
- Modify: `go.mod` (`golang.org/x/time` becomes a direct dependency via `go mod tidy`)
- Modify: `AGENTS.md`, `references/understanding.md`

**Interfaces:**
- Consumes: Task 1 (`db.KeyGrantRow`, `db.FindExchangeKey`, `db.ListKeyGrantsForIdentity`, `db.ExchangeTupleConstraint`, `EkInsertRow.IssuedVia/Requester`, `keygrant.Covering/Miss/Budget`); Task 2 (`keys.PeekAudience`, `jwt.Claims.JTI`, `keystore.ExchangeAudience`); Task 3 (`createReq.action/extra`, `mint`, `prodEnv`).
- Produces:
  - `envkeys.TokenReviewFunc func(ctx context.Context, token, audience string) (authnv1.TokenReviewStatus, error)`
  - `envkeys.ExchangeDeps{ Deps; ExchangeDB exchangeDB; TokenReview TokenReviewFunc; Signer jwt.Signer; TTL time.Duration }`, `envkeys.ExchangeHandler(ExchangeDeps) http.HandlerFunc`, `envkeys.ExchangeRequest`, `envkeys.ExchangeResponse`
  - `audit.ActionEkExchange = "platform.ek.exchange"`; outcomes `OutcomeIssued "issued"`, `OutcomeInvalidToken "invalid_token"`, `OutcomeNotGranted "not_granted"`, `OutcomeRateLimited "rate_limited"`, `OutcomeKeySuspended "key_suspended"`
  - `middleware.ClientIP(r *http.Request) string`
  - `platformapi.Deps.TokenReview envkeys.TokenReviewFunc`, `platformapi.Deps.KeyExchangeTTL time.Duration`
  - chart value `platformApi.keyExchange.tokenTTL` → env `ACH_KEY_EXCHANGE_TOKEN_TTL`

- [ ] **Step 1: Write the failing handler tests**

`internal/platformapi/envkeys/exchange_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package envkeys

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	authnv1 "k8s.io/api/authentication/v1"

	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/forwarder/jwt"
	"github.com/ackstorm/ach/internal/keystore"
	"github.com/ackstorm/ach/internal/platformapi/middleware"
)

const (
	exIssuer    = "https://ach.test/"
	exAudience  = "https://ach.test"
	exIdentity  = "ai.run@ackstorm.com"
	exRequester = "agents/agent-k8s-doctor"
	exUser      = "system:serviceaccount:agents:agent-k8s-doctor"
	okBody      = `{"identity":"AI.Run@ackstorm.com","environment":"prod"}`
)

// saToken is a compact JWS whose payload names aud: the guard reads it
// unverified, the faked TokenReview decides the rest.
func saToken(aud string) string {
	p := base64.RawURLEncoding.EncodeToString([]byte(`{"aud":["` + aud + `"],"sub":"` + exUser + `"}`))
	return "eyJhbGciOiJSUzI1NiJ9." + p + ".c2ln"
}

type fakeExchangeDB struct {
	grants   []db.KeyGrantRow
	existing *db.EkKeyInfo
	winner   *db.EkKeyInfo // answered once an insert race has been lost
	finds    int
}

func (f *fakeExchangeDB) ListKeyGrantsForIdentity(_ context.Context, _ string, identity string) ([]db.KeyGrantRow, error) {
	var out []db.KeyGrantRow
	for _, g := range f.grants {
		if g.Identity == identity {
			out = append(out, g)
		}
	}
	return out, nil
}

func (f *fakeExchangeDB) FindExchangeKey(context.Context, string, string, string) (*db.EkKeyInfo, error) {
	f.finds++
	if f.finds > 1 && f.winner != nil {
		return f.winner, nil
	}
	return f.existing, nil
}

func exGrant(name string, envs, sas []string, budget *float64) db.KeyGrantRow {
	return db.KeyGrantRow{Namespace: "ach", Name: name, Identity: exIdentity, Environments: envs, ServiceAccounts: sas, MaxBudget: budget}
}

func reviewAs(username string, podBound bool) TokenReviewFunc {
	return func(_ context.Context, _ string, aud string) (authnv1.TokenReviewStatus, error) {
		st := authnv1.TokenReviewStatus{Authenticated: true, Audiences: []string{aud}, User: authnv1.UserInfo{Username: username}}
		if podBound {
			st.User.Extra = map[string]authnv1.ExtraValue{extraPodName: {"agent-k8s-doctor-0"}, extraPodUID: {"uid-1"}}
		}
		return st, nil
	}
}

type exchangeFixture struct {
	deps         ExchangeDeps
	fdb          *fakeEkDB
	xdb          *fakeExchangeDB
	llm          *captureLiteLLM
	signer       *jwt.Ed25519Signer
	audit        *bytes.Buffer
	review       TokenReviewFunc
	reviews      int
	lastAudience string
	handler      http.HandlerFunc
}

func newExchangeFixture(t *testing.T) *exchangeFixture {
	t.Helper()
	fdb := &fakeEkDB{}
	deps, llm := newCreateDeps(fdb)
	deps.Issuer = exIssuer
	// 0 with fakeEkDB's zero allowance would refuse any CreateHandler call:
	// the exchange tests pass only because exchange never consults max_keys.
	deps.DefaultMaxKeys = 0
	f := &exchangeFixture{fdb: fdb, llm: llm, audit: &bytes.Buffer{}, review: reviewAs(exUser, true),
		xdb: &fakeExchangeDB{grants: []db.KeyGrantRow{exGrant("ai-run-doctor", []string{"prod"}, []string{exRequester}, nil)}}}
	deps.Audit = slog.New(slog.NewJSONHandler(f.audit, nil))
	f.signer = jwt.NewEd25519Signer()
	if err := jwt.LoadSeed(f.signer, "k1", make([]byte, 32)); err != nil {
		t.Fatal(err)
	}
	f.deps = ExchangeDeps{Deps: deps, ExchangeDB: f.xdb, Signer: f.signer, TTL: 15 * time.Minute,
		TokenReview: func(ctx context.Context, tok, aud string) (authnv1.TokenReviewStatus, error) {
			f.reviews++
			f.lastAudience = aud
			return f.review(ctx, tok, aud)
		}}
	return f
}

func (f *exchangeFixture) do(t *testing.T, bearer, body, remote string) *httptest.ResponseRecorder {
	t.Helper()
	if f.handler == nil {
		f.handler = ExchangeHandler(f.deps)
	}
	req := httptest.NewRequest(http.MethodPost, "/platform/keys/exchange", strings.NewReader(body))
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	if remote != "" {
		req.RemoteAddr = remote
	}
	req = req.WithContext(middleware.WithRequestID(req.Context(), "req_test"))
	rec := httptest.NewRecorder()
	f.handler.ServeHTTP(rec, req)
	return rec
}

func errorCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return body.Error.Code
}

func decodeExchange(t *testing.T, rec *httptest.ResponseRecorder) ExchangeResponse {
	t.Helper()
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var resp ExchangeResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestExchange_GuardsNeverCallTokenReview(t *testing.T) {
	f := newExchangeFixture(t)
	for name, bearer := range map[string]string{
		"no bearer":        "",
		"ek_ key":          "ek-" + strings.Repeat("a", 64),
		"foreign audience": saToken("https://elsewhere.example"),
		"ACH OAuth JWT":    saToken("ach"),
		"exchange JWT":     saToken(keystore.ExchangeAudience),
	} {
		rec := f.do(t, bearer, okBody, "")
		if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "invalid_token" {
			t.Errorf("%s: %d %s", name, rec.Code, rec.Body)
		}
	}
	if f.reviews != 0 {
		t.Fatalf("TokenReview called %d times for bearers that cannot be ours", f.reviews)
	}
}

func TestExchange_AudienceIgnoresTrailingSlash(t *testing.T) {
	for _, issuer := range []string{"https://ach.test/", "https://ach.test"} {
		f := newExchangeFixture(t)
		f.deps.Issuer = issuer
		decodeExchange(t, f.do(t, saToken("https://ach.test"), okBody, ""))
		if f.lastAudience != "https://ach.test" {
			t.Fatalf("issuer %q: TokenReview audience %q", issuer, f.lastAudience)
		}
	}
}

type unsyncedEnvStore struct{ fakeEnvStore }

func (*unsyncedEnvStore) AccessGroupSyncedFromRow(*db.EnvironmentRow) bool { return false }

func TestExchange_ChecksInOrder(t *testing.T) {
	cases := []struct {
		name   string
		setup  func(f *exchangeFixture)
		status int
		code   string
		reason string
	}{
		{"unbound token", func(f *exchangeFixture) { f.review = reviewAs(exUser, false) },
			http.StatusUnauthorized, "invalid_token", "not_pod_bound"},
		{"audience not reviewed", func(f *exchangeFixture) {
			ok := reviewAs(exUser, true)
			f.review = func(ctx context.Context, tok, aud string) (authnv1.TokenReviewStatus, error) {
				st, err := ok(ctx, tok, aud)
				st.Audiences = []string{"https://kubernetes.default.svc"}
				return st, err
			}
		}, http.StatusUnauthorized, "invalid_token", "audience"},
		{"not a ServiceAccount", func(f *exchangeFixture) { f.review = reviewAs("jane@ackstorm.com", true) },
			http.StatusUnauthorized, "invalid_token", "not_service_account"},
		{"unauthenticated", func(f *exchangeFixture) {
			f.review = func(context.Context, string, string) (authnv1.TokenReviewStatus, error) {
				return authnv1.TokenReviewStatus{}, nil
			}
		}, http.StatusUnauthorized, "invalid_token", "unauthenticated"},
		{"no grant", func(f *exchangeFixture) { f.xdb.grants = nil },
			http.StatusForbidden, "not_granted", "no_grant_for_identity"},
		{"grant names the Environment but not the requester", func(f *exchangeFixture) {
			f.xdb.grants = []db.KeyGrantRow{exGrant("g", []string{"prod"}, []string{"agents/other"}, nil)}
		}, http.StatusForbidden, "not_granted", "requester_not_granted"},
		{"lists only match when two grants are mixed", func(f *exchangeFixture) {
			f.xdb.grants = []db.KeyGrantRow{
				exGrant("a", []string{"prod"}, []string{"agents/other"}, nil),
				exGrant("b", []string{"dev"}, []string{exRequester}, nil),
			}
		}, http.StatusForbidden, "not_granted", "requester_not_granted"},
		{"Environment not synced", func(f *exchangeFixture) { f.deps.Store = &unsyncedEnvStore{fakeEnvStore{env: prodEnv()}} },
			http.StatusServiceUnavailable, "not_ready", ""},
		{"Environment missing", func(f *exchangeFixture) { f.deps.Store = &fakeEnvStore{} },
			http.StatusNotFound, "environment_not_found", ""},
		{"team mismatch", func(f *exchangeFixture) {
			f.deps.Store = &fakeEnvStore{env: &db.EnvironmentRow{Namespace: "ach", Name: "prod", AuthorizedTeams: []string{"finance"}}}
		}, http.StatusForbidden, "unauthorized_team", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newExchangeFixture(t)
			tc.setup(f)
			rec := f.do(t, saToken(exAudience), okBody, "")
			if rec.Code != tc.status || errorCode(t, rec) != tc.code {
				t.Fatalf("got %d %s, want %d %s", rec.Code, rec.Body, tc.status, tc.code)
			}
			if f.fdb.inserted != nil {
				t.Fatal("a refused exchange minted a key")
			}
			if tc.reason != "" && !strings.Contains(f.audit.String(), `"exchange.reason":"`+tc.reason+`"`) {
				t.Fatalf("audit lacks reason %q: %s", tc.reason, f.audit)
			}
		})
	}
}

func TestExchange_FirstMintsThenReuses(t *testing.T) {
	f := newExchangeFixture(t)
	resp := decodeExchange(t, f.do(t, saToken(exAudience), okBody, ""))
	if !strings.HasPrefix(resp.KeyID, "ekid_") || resp.ExpiresIn != 900 {
		t.Fatalf("%+v", resp)
	}
	ins := f.fdb.inserted
	if ins == nil || ins.IssuedVia != "exchange" || ins.Requester == nil || *ins.Requester != exRequester ||
		ins.OwnerEmail != exIdentity || ins.Name != "exchange:"+exRequester || ins.KeyID != resp.KeyID {
		t.Fatalf("inserted %+v", ins)
	}
	md := f.llm.lastKeyGenerateReq.Metadata
	if md["ach_issuer"] != exIssuer || md["ach_issued_via"] != "exchange" || md["ach_requester"] != exRequester {
		t.Fatalf("LiteLLM metadata %v", md)
	}
	sub, exp, err := f.signer.VerifyExp(resp.Token, exAudience, keystore.ExchangeAudience)
	if err != nil || sub != resp.KeyID {
		t.Fatalf("JWT sub=%q err=%v", sub, err)
	}
	if d := time.Until(exp); d < 14*time.Minute || d > 15*time.Minute {
		t.Fatalf("exp in %v", d)
	}

	f.xdb.existing = &db.EkKeyInfo{KeyID: resp.KeyID, Status: "active"}
	f.fdb.inserted = nil
	again := decodeExchange(t, f.do(t, saToken(exAudience), okBody, ""))
	if again.KeyID != resp.KeyID || f.fdb.inserted != nil || again.Token == resp.Token {
		t.Fatalf("second exchange must reuse %s with a new JWT: %+v, inserted %+v", resp.KeyID, again, f.fdb.inserted)
	}
}

func TestExchange_BudgetIsLowestCoveringGrant(t *testing.T) {
	f := newExchangeFixture(t)
	fifty, twenty := 50.0, 20.0
	f.xdb.grants = []db.KeyGrantRow{
		exGrant("a", []string{"prod"}, []string{exRequester}, &fifty),
		exGrant("b", []string{"prod"}, []string{exRequester}, &twenty),
	}
	resp := decodeExchange(t, f.do(t, saToken(exAudience), okBody, ""))
	if b, ok := f.llm.upsertedTags["key:"+resp.KeyID]; !ok || b.MaxBudget != 20 {
		t.Fatalf("key budget = %+v (set=%v), want 20", b, ok)
	}
}

type raceEkDB struct{ fakeEkDB }

func (*raceEkDB) InsertEnvironmentKey(context.Context, db.EkInsertRow) error {
	return fmt.Errorf("db: InsertEnvironmentKey(x): %w", &pgconn.PgError{Code: "23505", ConstraintName: db.ExchangeTupleConstraint})
}

type revokeCapture struct {
	*captureLiteLLM
	revoked []string
}

func (c *revokeCapture) RevokeKey(_ context.Context, token string) error {
	c.revoked = append(c.revoked, token)
	return nil
}

func TestExchange_RaceLoserRevokesItsKeyAndAnswersWithWinner(t *testing.T) {
	f := newExchangeFixture(t)
	f.deps.DB = &raceEkDB{}
	llm := &revokeCapture{captureLiteLLM: f.llm}
	f.deps.LiteLLM = llm
	f.xdb.winner = &db.EkKeyInfo{KeyID: "ekid_winner", Status: "active"}
	resp := decodeExchange(t, f.do(t, saToken(exAudience), okBody, ""))
	if resp.KeyID != "ekid_winner" {
		t.Fatalf("key_id %s, want the winner's", resp.KeyID)
	}
	if len(llm.revoked) != 1 {
		t.Fatalf("the race loser must revoke the LiteLLM key it minted: %v", llm.revoked)
	}
}

func TestExchange_SuspendedKeyIsRefused(t *testing.T) {
	f := newExchangeFixture(t)
	f.xdb.existing = &db.EkKeyInfo{KeyID: "ekid_s", Status: "suspended"}
	rec := f.do(t, saToken(exAudience), okBody, "")
	if rec.Code != http.StatusConflict || errorCode(t, rec) != "key_suspended" {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

func TestExchange_RateLimited(t *testing.T) {
	f := newExchangeFixture(t)
	f.review = func(context.Context, string, string) (authnv1.TokenReviewStatus, error) {
		return authnv1.TokenReviewStatus{}, nil // every admitted request is a cheap 401
	}
	var last *httptest.ResponseRecorder
	for i := 0; i <= exchangePerClientBurst; i++ {
		last = f.do(t, saToken(exAudience), okBody, fmt.Sprintf("10.0.0.7:%d", 40000+i)) // new port, same client
	}
	if last.Code != http.StatusTooManyRequests || errorCode(t, last) != "rate_limited" {
		t.Fatalf("request %d: %d %s", exchangePerClientBurst+1, last.Code, last.Body)
	}
	if f.reviews != exchangePerClientBurst {
		t.Fatalf("TokenReview calls = %d, want %d", f.reviews, exchangePerClientBurst)
	}
	if rec := f.do(t, saToken(exAudience), okBody, "10.0.0.8:1"); rec.Code == http.StatusTooManyRequests {
		t.Fatal("another client must not share the first one's budget")
	}

	g := newExchangeFixture(t)
	g.review = f.review
	limited := false
	for i := 0; i < exchangeGlobalBurst+10; i++ {
		if g.do(t, saToken(exAudience), okBody, fmt.Sprintf("10.1.%d.%d:1", i/250, i%250)).Code == http.StatusTooManyRequests {
			limited = true
		}
	}
	if !limited {
		t.Fatal("the global limit never answered 429")
	}
}

func TestExchange_AuditOneEventPerRequestWithoutCredentials(t *testing.T) {
	f := newExchangeFixture(t)
	resp := decodeExchange(t, f.do(t, saToken(exAudience), okBody, ""))
	f.xdb.grants = nil
	f.do(t, saToken(exAudience), okBody, "")

	lines := strings.Split(strings.TrimSpace(f.audit.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("want 2 audit events, got %d: %s", len(lines), f.audit)
	}
	for _, want := range []string{`"action":"platform.ek.exchange"`, `"outcome":"issued"`, `"key.id":"` + resp.KeyID + `"`,
		`"actor":"` + exUser + `"`, `"exchange.requester":"` + exRequester + `"`, `"exchange.pod_uid":"uid-1"`} {
		if !strings.Contains(lines[0], want) {
			t.Errorf("issued event lacks %s: %s", want, lines[0])
		}
	}
	if !strings.Contains(lines[1], `"outcome":"not_granted"`) {
		t.Errorf("denied event: %s", lines[1])
	}
	for _, secret := range []string{saToken(exAudience), resp.Token} {
		if strings.Contains(f.audit.String(), secret) {
			t.Fatal("a credential reached the audit log")
		}
	}
}
```

- [ ] **Step 2: Write the failing server test**

Append to `internal/platformapi/server_test.go` (add imports `authnv1 "k8s.io/api/authentication/v1"`):

```go
func TestServer_MountsKeyExchangeOutsideAuthn(t *testing.T) {
	mr := miniredis.RunT(t)
	deps := newTestDeps(t, &fakeResolver{})
	deps.OAuth = &auth.OAuthDeps{
		Store:  &auth.OAuthStore{RDB: redis.NewClient(&redis.Options{Addr: mr.Addr()})},
		Signer: jwt.NewEd25519Signer(), Issuer: "https://ach.example.com", Audience: "ach",
		AccessTTL: time.Hour, RefreshTTL: time.Hour,
	}
	deps.TokenReview = func(context.Context, string, string) (authnv1.TokenReviewStatus, error) {
		t.Error("the guard must answer before TokenReview")
		return authnv1.TokenReviewStatus{}, nil
	}
	deps.KeyExchangeTTL = 15 * time.Minute
	h := New(deps)

	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/platform/keys/exchange", strings.NewReader(`{}`)))
	if w.Code != http.StatusUnauthorized || !strings.Contains(w.Body.String(), "invalid_token") {
		t.Fatalf("exchange: %d %s (Authn would answer missing_key)", w.Code, w.Body)
	}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/platform/keys", strings.NewReader(`{}`)))
	if w.Code != http.StatusUnauthorized || !strings.Contains(w.Body.String(), "missing_key") {
		t.Fatalf("the rest of /platform/keys must stay behind Authn: %d %s", w.Code, w.Body)
	}
}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `LOG=$(mktemp); timeout 900 make test-unit-pkg PKG='./internal/platformapi/envkeys ./internal/platformapi' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|undefined|unknown field' "$LOG"`
Expected: FAIL — `undefined: TokenReviewFunc`, `undefined: ExchangeHandler`, `unknown field TokenReview in struct literal of type Deps`.

- [ ] **Step 4: Audit vocabulary**

In `internal/audit/events.go` add after `ActionAdminUserLimits`:

```go
	ActionEkExchange           = "platform.ek.exchange"
```

and before the closing `)` of the Outcome block:

```go
	// POST /platform/keys/exchange outcomes.
	OutcomeIssued       = "issued"
	OutcomeInvalidToken = "invalid_token"
	OutcomeNotGranted   = "not_granted"
	OutcomeRateLimited  = "rate_limited"
	OutcomeKeySuspended = "key_suspended"
```

In `internal/audit/events_test.go` `TestEventConstantsAreStable`, add rows:

```go
		{"ActionEkExchange", audit.ActionEkExchange, "platform.ek.exchange"},
		{"OutcomeIssued", audit.OutcomeIssued, "issued"},
		{"OutcomeInvalidToken", audit.OutcomeInvalidToken, "invalid_token"},
		{"OutcomeNotGranted", audit.OutcomeNotGranted, "not_granted"},
		{"OutcomeRateLimited", audit.OutcomeRateLimited, "rate_limited"},
		{"OutcomeKeySuspended", audit.OutcomeKeySuspended, "key_suspended"},
```

- [ ] **Step 5: Export the client address helper**

In `internal/platformapi/middleware/middleware.go` rename `clientIP` to `ClientIP` (definition at :185 and the call at :175) and change its doc's last sentence to "Used for the audit source.ip attribute (G20) and the key-exchange rate limit."

- [ ] **Step 6: Exchange support in `mint`**

In `internal/platformapi/envkeys/handler.go`:

1. Add to `createReq` after `extra`:

```go
	// requester ("<namespace>/<serviceaccount>") and xdb are set only by
	// POST /platform/keys/exchange: they make mint issue an exchange key.
	requester string
	xdb       exchangeDB
```

2. In `mint`, right after the `keyReq := &litellm.KeyGenerateRequest{…}` literal:

```go
	if cr.requester != "" {
		keyReq.Metadata["ach_issued_via"] = "exchange"
		keyReq.Metadata["ach_requester"] = cr.requester
	}
```

3. Right after the `insertRow := db.EkInsertRow{…}` literal:

```go
	if cr.requester != "" {
		insertRow.IssuedVia = "exchange"
		insertRow.Requester = &cr.requester
	}
```

4. Immediately before `if insertErr != nil {` (the compensation block):

```go
	if insertErr != nil && cr.xdb != nil && classifyInsertError(insertErr) == insertErrExchangeTuple {
		// A concurrent first exchange for the same tuple won the partial
		// unique index: drop the LiteLLM key minted here, answer with the
		// winner's row.
		compCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		if cleanupErr := deps.LiteLLM.RevokeKey(compCtx, llToken); cleanupErr != nil {
			deps.Logger.Error("envkeys.exchange: race-loser RevokeKey failed", "key_id", keyID, "err", cleanupErr)
		}
		cancel()
		winner, err := cr.xdb.FindExchangeKey(ctx, cr.keyCtx.OwnerEmail, cr.req.Environment, cr.requester)
		if err == nil && winner == nil {
			err = errors.New("exchange tuple taken but no live row found")
		}
		if err != nil {
			cr.emitInternalError("envkeys.exchange: read race winner", err)
			return "", "", false
		}
		return winner.KeyID, "", true
	}
```

5. In the `insertErrClass` const block add `insertErrExchangeTuple` after `insertErrEkidCollision`, and in `classifyInsertError` before the final `return insertErrOther`:

```go
	if pgErr.ConstraintName == db.ExchangeTupleConstraint {
		return insertErrExchangeTuple
	}
```

- [ ] **Step 7: Write the exchange handler**

`internal/platformapi/envkeys/exchange.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package envkeys

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"net"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"

	"golang.org/x/time/rate"
	authnv1 "k8s.io/api/authentication/v1"

	"github.com/ackstorm/ach/internal/audit"
	"github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/forwarder/jwt"
	"github.com/ackstorm/ach/internal/keygrant"
	"github.com/ackstorm/ach/internal/keys"
	"github.com/ackstorm/ach/internal/keystore"
	"github.com/ackstorm/ach/internal/litellm"
	"github.com/ackstorm/ach/internal/platformapi/middleware"
	"github.com/ackstorm/ach/internal/platformapi/render"
)

// TokenReviewFunc asks the Kubernetes API server to review a ServiceAccount
// token for one audience (authentication.k8s.io/v1 TokenReview).
type TokenReviewFunc func(ctx context.Context, token, audience string) (authnv1.TokenReviewStatus, error)

// exchangeDB is the exchange handler's Postgres seam beyond dbOps.
type exchangeDB interface {
	ListKeyGrantsForIdentity(ctx context.Context, ns, identity string) ([]db.KeyGrantRow, error)
	FindExchangeKey(ctx context.Context, owner, environment, requester string) (*db.EkKeyInfo, error)
}

// ExchangeDeps is Deps plus what POST /platform/keys/exchange alone needs.
type ExchangeDeps struct {
	Deps
	ExchangeDB  exchangeDB
	TokenReview TokenReviewFunc
	Signer      jwt.Signer
	// TTL is the exchanged JWT's lifetime (ACH_KEY_EXCHANGE_TOKEN_TTL).
	TTL time.Duration
}

// ExchangeRequest is the POST /platform/keys/exchange body.
type ExchangeRequest struct {
	Identity    string `json:"identity"`
	Environment string `json:"environment"`
}

// ExchangeResponse carries the JWT. Clients schedule the next exchange from
// ExpiresIn, never from the token's wall-clock exp.
type ExchangeResponse struct {
	Token     string `json:"token"`
	ExpiresIn int    `json:"expires_in"`
	KeyID     string `json:"key_id"`
}

const (
	saUserPrefix = "system:serviceaccount:"
	extraPodName = "authentication.kubernetes.io/pod-name"
	extraPodUID  = "authentication.kubernetes.io/pod-uid"

	// The limits guard the API server, not ACH: each admitted request costs
	// one TokenReview. A healthy agent exchanges every ~12 minutes; the
	// per-client burst covers a node's worth of agents restarting at once
	// behind one egress address.
	exchangePerClientRate  = 1
	exchangePerClientBurst = 10
	exchangeGlobalRate     = 10
	exchangeGlobalBurst    = 50
	exchangeMaxClients     = 10000
)

// ExchangeHandler serves POST /platform/keys/exchange. A pod-bound
// ServiceAccount token plus a KeyGrant naming its ServiceAccount, the identity
// and the Environment together buys a short-lived ACH JWT whose subject is
// the tuple's one stable ek_, minted on first use by the create path's mint.
// The raw ek_ never leaves ACH.
//
// Two guards never reach the API server: the bearer must be a compact JWS
// whose unverified aud names this ACH (401 invalid_token), then the
// per-client and global rate limit (429) — after the first guard, so junk
// cannot drain the budget that protects TokenReview. Then, first failure
// answers: TokenReview (401 invalid_token), one grant allows the tuple (403
// not_granted, one code for every miss; the audit names which), the
// Environment exists and is synced (404 / 503), the identity's teams
// intersect authorizedTeams (403 unauthorized_team). Exactly one
// platform.ek.exchange audit event per request, never carrying a token.
func ExchangeHandler(d ExchangeDeps) http.HandlerFunc {
	aud := strings.TrimRight(d.Issuer, "/")
	lim := newExchangeLimiter()
	return func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		reqID := middleware.RequestIDFromCtx(ctx)
		ev := audit.Event{Action: audit.ActionEkExchange, RequestID: reqID, Extra: map[string]string{}}
		deny := func(status int, code, msg, reason string) {
			ev.Outcome = code
			if reason != "" {
				ev.Extra["exchange.reason"] = reason
			}
			audit.EmitAudit(ctx, d.Audit, ev)
			render.Error(w, status, code, msg, reqID)
		}

		token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !ok || !keys.LooksLikeJWS(token) || !slices.Contains(keys.PeekAudience(token), aud) {
			deny(http.StatusUnauthorized, audit.OutcomeInvalidToken, "a ServiceAccount token issued for this ACH is required", "bearer")
			return
		}
		if !lim.allow(middleware.ClientIP(r)) {
			deny(http.StatusTooManyRequests, audit.OutcomeRateLimited, "too many exchange requests", "")
			return
		}
		var req ExchangeRequest
		dec := json.NewDecoder(r.Body)
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil || req.Identity == "" || req.Environment == "" {
			deny(http.StatusBadRequest, codeInvalidArgument, "identity and environment required", "")
			return
		}
		identity := litellm.NormalizeEmail(req.Identity)
		ev.Extra["exchange.identity"] = identity
		ev.Target = &audit.Target{Kind: "environment", Name: req.Environment}

		st, err := d.TokenReview(ctx, token, aud)
		if err != nil {
			d.Logger.Error("envkeys.exchange: TokenReview failed", "err", err)
			deny(http.StatusServiceUnavailable, audit.OutcomeNotReady, "token review unavailable", "tokenreview_error")
			return
		}
		requester, why := reviewedRequester(st, aud)
		if why != "" {
			deny(http.StatusUnauthorized, audit.OutcomeInvalidToken, "invalid ServiceAccount token", why)
			return
		}
		ev.Actor = st.User.Username
		ev.Extra["exchange.requester"] = requester
		ev.Extra["exchange.pod_name"] = firstExtra(st.User.Extra[extraPodName])
		ev.Extra["exchange.pod_uid"] = firstExtra(st.User.Extra[extraPodUID])

		grants, err := d.ExchangeDB.ListKeyGrantsForIdentity(ctx, d.Namespace, identity)
		if err != nil {
			d.Logger.Error("envkeys.exchange: grant read failed", "err", err)
			deny(http.StatusInternalServerError, audit.OutcomeInternalError, "internal error", "")
			return
		}
		covering := keygrant.Covering(grants, identity, req.Environment, requester)
		if len(covering) == 0 {
			deny(http.StatusForbidden, audit.OutcomeNotGranted, "no KeyGrant allows this exchange", keygrant.Miss(grants, req.Environment))
			return
		}

		// Environment, team and mint are the create path's own steps.
		cr := &createReq{
			deps: d.Deps, w: w, ctx: ctx, action: audit.ActionEkExchange, extra: ev.Extra,
			actor: ev.Actor, reqID: reqID, target: ev.Target, requester: requester, xdb: d.ExchangeDB,
			keyCtx: middleware.KeyContext{OwnerEmail: identity},
			req:    CreateRequest{Environment: req.Environment, Name: "exchange:" + requester},
		}
		if b := keygrant.Budget(covering); b != nil {
			cr.req.Budget = &BudgetRequest{MaxBudget: &b.MaxBudget, BudgetDuration: b.BudgetDuration}
		}
		env, handled := cr.validateAndLoadEnv()
		if handled {
			return
		}
		if cr.validateTeamMembership(env) {
			return
		}
		keyID, ok := cr.stableKey(env)
		if !ok {
			return
		}
		signed, err := d.Signer.Sign(ctx, jwt.Claims{
			Iss: aud, Sub: keyID, Aud: keystore.ExchangeAudience, TTL: d.TTL, JTI: rand.Text(),
		})
		if err != nil {
			cr.emitInternalError("envkeys.exchange: sign failed", err)
			return
		}
		ev.Outcome, ev.KeyID = audit.OutcomeIssued, keyID
		audit.EmitAudit(ctx, d.Audit, ev)
		render.JSON(w, http.StatusOK, ExchangeResponse{Token: signed, ExpiresIn: int(d.TTL / time.Second), KeyID: keyID})
	}
}

// stableKey returns the tuple's live exchange key, minting it on first use.
// The key's budget (lowest covering grant) is written by mint at creation;
// the operator's KeyGrant sweep keeps it current afterwards.
func (cr *createReq) stableKey(env *db.EnvironmentRow) (string, bool) {
	row, err := cr.xdb.FindExchangeKey(cr.ctx, cr.keyCtx.OwnerEmail, env.Name, cr.requester)
	if err != nil {
		cr.emitInternalError("envkeys.exchange: FindExchangeKey failed", err)
		return "", false
	}
	if row != nil {
		if row.Status != statusActive {
			// Suspended by its owner: a JWT for it could never authenticate.
			audit.EmitAudit(cr.ctx, cr.deps.Audit, audit.Event{
				Action: cr.action, Outcome: audit.OutcomeKeySuspended, Actor: cr.actor,
				RequestID: cr.reqID, KeyID: row.KeyID, Target: cr.target, Extra: cr.extra,
			})
			render.Error(cr.w, http.StatusConflict, audit.OutcomeKeySuspended, "the exchange key for this grant is suspended", cr.reqID)
			return "", false
		}
		return row.KeyID, true
	}
	userID, handled := cr.provisionUser()
	if handled {
		return "", false
	}
	keyID, _, ok := cr.mint(env, userID)
	return keyID, ok
}

// reviewedRequester turns a TokenReview answer into "<namespace>/<name>", or
// names why the token is refused. Only a pod-bound token carries the pod
// extras.
func reviewedRequester(st authnv1.TokenReviewStatus, aud string) (requester, why string) {
	switch {
	case !st.Authenticated:
		return "", "unauthenticated"
	case !slices.Contains(st.Audiences, aud):
		return "", "audience"
	}
	rest, isSA := strings.CutPrefix(st.User.Username, saUserPrefix)
	ns, name, ok := strings.Cut(rest, ":")
	if !isSA || !ok || ns == "" || name == "" {
		return "", "not_service_account"
	}
	if firstExtra(st.User.Extra[extraPodName]) == "" || firstExtra(st.User.Extra[extraPodUID]) == "" {
		return "", "not_pod_bound"
	}
	return ns + "/" + name, ""
}

func firstExtra(v authnv1.ExtraValue) string {
	if len(v) == 0 {
		return ""
	}
	return v[0]
}

// exchangeLimiter is a per-client token bucket in front of a global one.
// ponytail: the client map is cleared when it reaches exchangeMaxClients and
// is keyed on the first X-Forwarded-For hop, which a caller can forge — the
// global bucket is the hard bound on TokenReview calls. An LRU keyed on a
// trusted proxy hop is the upgrade if per-client fairness ever matters.
type exchangeLimiter struct {
	mu      sync.Mutex
	global  *rate.Limiter
	clients map[string]*rate.Limiter
}

func newExchangeLimiter() *exchangeLimiter {
	return &exchangeLimiter{
		global:  rate.NewLimiter(exchangeGlobalRate, exchangeGlobalBurst),
		clients: map[string]*rate.Limiter{},
	}
}

func (l *exchangeLimiter) allow(addr string) bool {
	if host, _, err := net.SplitHostPort(addr); err == nil {
		addr = host // RemoteAddr carries a per-connection port
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	c, ok := l.clients[addr]
	if !ok {
		if len(l.clients) >= exchangeMaxClients {
			clear(l.clients)
		}
		c = rate.NewLimiter(exchangePerClientRate, exchangePerClientBurst)
		l.clients[addr] = c
	}
	return c.Allow() && l.global.Allow()
}
```

- [ ] **Step 8: Mount the route and wire the service**

`internal/platformapi/adapters.go` — append:

```go
func (a *envkeysDBAdapter) ListKeyGrantsForIdentity(ctx context.Context, ns, identity string) ([]db.KeyGrantRow, error) {
	return db.ListKeyGrantsForIdentity(ctx, a.pool, ns, identity)
}

func (a *envkeysDBAdapter) FindExchangeKey(ctx context.Context, owner, environment, requester string) (*db.EkKeyInfo, error) {
	return db.FindExchangeKey(ctx, a.pool, owner, environment, requester)
}
```

`internal/platformapi/server.go` — add to `Deps` after `VerifyAccessToken`:

```go
	// TokenReview reviews ServiceAccount tokens for POST /platform/keys/exchange;
	// nil (no in-cluster config) leaves the route unmounted.
	TokenReview envkeys.TokenReviewFunc
	// KeyExchangeTTL is the exchanged JWT's lifetime (ACH_KEY_EXCHANGE_TOKEN_TTL).
	KeyExchangeTTL time.Duration
```

In `New`, move the `envkeysDeps := envkeys.Deps{…}` literal and its `if deps.OAuth != nil { envkeysDeps.EndOAuthSessions = … }` from inside the Authn `r.Group` to just before `// Authenticated subtree`, and add after them:

```go
	// POST /platform/keys/exchange: the ServiceAccount token IS the credential,
	// so the route sits outside the pk_/ek_ Authn subtree. It signs with the
	// OAuth AS's signer.
	if deps.TokenReview != nil && deps.OAuth != nil {
		r.Post("/platform/keys/exchange", envkeys.ExchangeHandler(envkeys.ExchangeDeps{
			Deps: envkeysDeps, ExchangeDB: newEnvkeysDB(deps.Pool),
			TokenReview: deps.TokenReview, Signer: deps.OAuth.Signer, TTL: deps.KeyExchangeTTL,
		}))
	}
```

(the `envkeys.MountKeys(r, envkeysDeps)` call stays inside the group).

`cmd/ach/cmd/tokenreview.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package cmd

import (
	"context"

	authnv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	authnv1client "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"

	"github.com/ackstorm/ach/internal/platformapi/envkeys"
)

// inClusterTokenReview returns the TokenReview call POST /platform/keys/exchange
// makes as the platform-api ServiceAccount (chart: system:auth-delegator). An
// error means no in-cluster config, and the route stays unmounted.
func inClusterTokenReview() (envkeys.TokenReviewFunc, error) {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	c, err := authnv1client.NewForConfig(cfg)
	if err != nil {
		return nil, err
	}
	return func(ctx context.Context, token, audience string) (authnv1.TokenReviewStatus, error) {
		tr, err := c.TokenReviews().Create(ctx, &authnv1.TokenReview{
			Spec: authnv1.TokenReviewSpec{Token: token, Audiences: []string{audience}},
		}, metav1.CreateOptions{})
		if err != nil {
			return authnv1.TokenReviewStatus{}, err
		}
		return tr.Status, nil
	}, nil
}
```

`cmd/ach/cmd/platform_api.go`:
- add to `platformAPIConfig` after `OAuthRefreshTTL`: `KeyExchangeTTL time.Duration // ACH_KEY_EXCHANGE_TOKEN_TTL, default 15m, minimum 1m`
- in `validatePlatformAPIConfig`, after the `ACH_OAUTH_REFRESH_TTL` block:

```go
	if cfg.KeyExchangeTTL, err = config.MustEnvDurationAtLeast("ACH_KEY_EXCHANGE_TOKEN_TTL", 15*time.Minute, time.Minute); err != nil {
		return nil, err
	}
```

- before `out.server = platformapi.Deps{`:

```go
	tokenReview, trErr := inClusterTokenReview()
	if trErr != nil {
		logger.Warn("POST /platform/keys/exchange disabled: no in-cluster Kubernetes config", "err", trErr)
	}
```

- in the `platformapi.Deps{…}` literal: `TokenReview: tokenReview, KeyExchangeTTL: cfg.KeyExchangeTTL,`

Make `golang.org/x/time` a direct requirement:

Run: `LOG=$(mktemp); timeout 600 ./scripts/dev.sh go mod tidy > "$LOG" 2>&1; echo "EXIT=$?"; git diff --stat go.mod go.sum`
Expected: `EXIT=0`; `go.mod` moves `golang.org/x/time` to the direct `require` block; no new module.

- [ ] **Step 9: Chart — TokenReview binding and TTL value**

In `deploy/helm/ach/templates/platform-api-rbac.yaml` replace the header comment's "Result: platform-api holds zero K8s privileges. The ServiceAccount stays bound to an empty ClusterRole …" paragraph with:

```yaml
# Result: platform-api's own ClusterRole stays empty. Its one K8s privilege
# is TokenReview for POST /platform/keys/exchange, through the built-in
# system:auth-delegator ClusterRole (binding below).
```

and append before the closing `{{- end }}`:

```yaml
---
# TokenReview for POST /platform/keys/exchange. system:auth-delegator grants
# create on tokenreviews and subjectaccessreviews, nothing else. Cluster-scoped
# because TokenReview is.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ .Release.Name }}-platform-api-auth-delegator
  labels:
    {{- include "ach.commonLabels" . | nindent 4 }}
    app.kubernetes.io/component: platform-api
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
- kind: ServiceAccount
  name: ach-platform-api
  namespace: {{ .Release.Namespace }}
```

In `deploy/helm/ach/values.yaml`, inside `platformApi:` after the `userDefaults:` block:

```yaml
  # keyExchange: POST /platform/keys/exchange trades a pod-bound ServiceAccount
  # token (plus a KeyGrant) for a short-lived ACH JWT. tokenTTL is that JWT's
  # lifetime (Go duration, minimum 1m).
  keyExchange:
    tokenTTL: 15m
```

In `deploy/helm/ach/templates/platform-api-deployment.yaml`, after the `ACH_USER_MAX_KEYS` env entry:

```yaml
            - name: ACH_KEY_EXCHANGE_TOKEN_TTL
              value: {{ (.Values.platformApi.keyExchange).tokenTTL | default "15m" | quote }}
```

In `scripts/helm-render-check.sh`, before the final `echo`, add and change the final line to `echo "helm-render-check OK (7 topologies)"`:

```bash
# 7. Key exchange: platform-api may call TokenReview and nothing else.
grep -q "name: system:auth-delegator" <<<"$(render --show-only templates/platform-api-rbac.yaml)" \
  || fail "default: platform-api lacks the system:auth-delegator binding (TokenReview)"
grep -A1 "name: ACH_KEY_EXCHANGE_TOKEN_TTL" <<<"$(render)" | grep -q 'value: "15m"' \
  || fail "default: ACH_KEY_EXCHANGE_TOKEN_TTL must be \"15m\""
```

- [ ] **Step 10: Docs**

`AGENTS.md`:
1. Replace "the platform-api's only remaining k8s\ntouchpoint is the Dex leg of the OAuth AS." with:

```markdown
the platform-api's only k8s
touchpoints are the Dex leg of the OAuth AS and TokenReview for
`POST /platform/keys/exchange` (a cluster-wide `system:auth-delegator`
ClusterRoleBinding; outside a cluster the route is not mounted).
```

2. In the sentence "uses ACH's public API only: `POST /platform/hydrate` and `GET /v2/model/info` with an `ek_` in `x-ach-key`, plus the model and MCP routes." replace "with an `ek_` in `x-ach-key`" by "with an `ek_` (or an exchanged ACH JWT, `POST /platform/keys/exchange`) in `x-ach-key`".
3. After the "Per-user `ek_` key ceiling" paragraph (ends "…takes effect on the next create without a cache delay."), add:

```markdown
Key exchange (`POST /platform/keys/exchange`, outside the `pk_`/`ek_` Authn
subtree): `Authorization: Bearer <pod-bound ServiceAccount token>` whose
audience is `ACH_BASE_URL` without a trailing slash, body
`{"identity","environment"}`. Guards before any API-server call: a compact
JWS whose unverified `aud` names that audience (else 401 `invalid_token`),
then a per-client (first `X-Forwarded-For` hop) and global rate limit (429
`rate_limited`). Then, first failure answers: TokenReview (authenticated,
audience, `system:serviceaccount:`, pod-name + pod-uid extras; 401
`invalid_token`), ONE `KeyGrant` naming identity + Environment + `<ns>/<sa>`
together (403 `not_granted` for every miss; the audit names which), the
Environment exists and is synced (404/503), the identity's teams ∩
`authorizedTeams` (403 `unauthorized_team`). Success: the tuple's ONE stable
`ek_` (`issued_via='exchange'`, `requester`, name `exchange:<ns>/<sa>`,
minted on first use by the create path's `mint`; the partial unique index
`environment_keys_exchange_tuple_uniq` makes racing first exchanges converge;
it never counts against `max_keys`; a suspended one is 409 `key_suspended`;
its `key:<id>` budget is the lowest covering `spec.budget`) and a JWT
`iss=ACH_BASE_URL aud=ach-ek sub=<ekid_> jti`, lifetime
`platformApi.keyExchange.tokenTTL` → `ACH_KEY_EXCHANGE_TOKEN_TTL` (15m, min
1m), answered as `{token, expires_in, key_id}`. The raw `ek_` never leaves
ACH. `keystore.NewExchangeResolver` (forwarder, content-service,
platform-api; wraps the OAuth resolver inside the Redis cache) accepts that JWT
wherever an `ek_` goes (`x-ach-key` or `Authorization`) only while a grant
still allows its tuple. One `platform.ek.exchange` audit event per request,
never a token.
```

`references/understanding.md`:
- In the platform-api row replace "NO k8s client." with "Only k8s client: TokenReview for `POST /platform/keys/exchange` (`system:auth-delegator`)."
- Append to the "**Rate limiting / DoS**" bullet: " One exception: `POST /platform/keys/exchange` rate-limits itself, because each admitted request costs a TokenReview."

`internal/platformapi/opencode/skill/SKILL.md`: unchanged (no `ach-cli` command, flag or output changes) — verify with `git diff --stat -- internal/platformapi/opencode` showing nothing.

- [ ] **Step 11: Run the tests, build and render check**

Run: `LOG=$(mktemp); timeout 900 make test-unit-pkg PKG='./internal/platformapi/envkeys ./internal/platformapi ./internal/platformapi/middleware ./internal/audit' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: four `ok` lines, `EXIT=0`.

Run: `LOG=$(mktemp); timeout 900 ./scripts/dev.sh go build ./... > "$LOG" 2>&1; echo "EXIT=$?"; cat "$LOG"`
Expected: `EXIT=0`.

Run: `LOG=$(mktemp); timeout 600 make helm-render-check > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 3 "$LOG"`
Expected: `helm-render-check OK (7 topologies)`, `EXIT=0`.

- [ ] **Step 12: Commit**

```bash
git add internal/platformapi/ internal/audit/ cmd/ach/cmd/tokenreview.go cmd/ach/cmd/platform_api.go \
  deploy/helm/ach/templates/platform-api-rbac.yaml deploy/helm/ach/templates/platform-api-deployment.yaml \
  deploy/helm/ach/values.yaml scripts/helm-render-check.sh go.mod go.sum AGENTS.md references/understanding.md
git commit -m "feat(platform-api): exchange a ServiceAccount token for an ACH JWT"
```

---

### Task 5: `KeyGrant` CRD and its operator reconciler

**Files:**
- Create: `api/ach/v1alpha1/keygrant_types.go`
- Regenerate: `api/ach/v1alpha1/zz_generated.deepcopy.go`, `config/crd/bases/ach.ackstorm.ai_keygrants.yaml`, `config/rbac/role.yaml`, `deploy/helm/ach/crd-sources/ach.ackstorm.ai_keygrants.yaml`, `docs/api-reference/ach.ackstorm.ai.md`
- Create: `config/samples/ach_v1alpha1_keygrant.yaml`; Modify: `config/samples/kustomization.yaml`
- Create: `internal/controller/ach/keygrant_controller.go`, `internal/controller/ach/keygrant_controller_test.go`
- Modify: `internal/controller/ach/finalizers.go`, `internal/controller/ach/suite_test.go:61-72,400-408`
- Modify: `internal/operator/resync/runnable.go:24-35,92-111`, `internal/operator/resync/runnable_test.go`
- Modify: `cmd/ach/cmd/operator.go:374-400,541-570`
- Modify: `AGENTS.md`, `references/understanding.md`, `references/adding-a-cr-kind.md`

**Interfaces:**
- Consumes: Task 1 (`db.KeyGrantRow` + helpers, `db.ListExchangeKeys`, `db.RevokeEnvironmentKey`, `keygrant.Plan`); `litellm.Client` (`UserInfoByEmail`, `RevokeKey`, `UpsertTagBudget`), `litellm.DeleteKeyBudget`, `litellm.KeyBudgetTag`; `setExternalRefCondition`, `retryStatusUpdate`, `setupWithResync` (existing in package `ach`).
- Produces:
  - `v1alpha1.KeyGrant{Spec KeyGrantSpec{Identity string; Environments []string; Requesters KeyGrantRequesters{ServiceAccounts []string}; Budget *BudgetBlock}; Status KeyGrantStatus{ObservedGeneration int64; Conditions []metav1.Condition}}`, `KeyGrantList`
  - `ach.KeyGrantStore` interface `{ UpsertKeyGrant; DeleteKeyGrant; ListKeyGrants; ListExchangeKeys; RevokeEnvironmentKey }`, `ach.NewKeyGrantStore(*pgxpool.Pool) KeyGrantStore`
  - `ach.KeyGrantReconciler{Client; Scheme; LiteLLM litellm.Client; Namespace string; Log logr.Logger; Store KeyGrantStore; ResyncSource chan event.GenericEvent}`
  - `resync.Channels.KeyGrant chan<- event.GenericEvent`
  - Condition `Ready`: `True/Granted`, `False/IdentityUnknown`, `False/EnvironmentMissing`

- [ ] **Step 1: Write the CRD types**

`api/ach/v1alpha1/keygrant_types.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// KeyGrantRequesters names who may exchange for the grant's identity. It is a
// block so other requester kinds can sit beside ServiceAccounts.
type KeyGrantRequesters struct {
	// ServiceAccounts are exact "<namespace>/<name>" ServiceAccounts, both
	// parts DNS-1123. No patterns.
	//
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MaxLength=317
	// +kubebuilder:validation:items:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?/[a-z0-9]([-a-z0-9.]*[a-z0-9])?$`
	// +listType=set
	ServiceAccounts []string `json:"serviceAccounts"`
}

// KeyGrantSpec allows POST /platform/keys/exchange for one identity in every
// combination of its Environments and Requesters — and nothing that would
// only match by combining lists from different grants.
type KeyGrantSpec struct {
	// Identity is exactly one ACH identity: an email, lower-case. No patterns.
	//
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MaxLength=254
	// +kubebuilder:validation:Pattern=`^[a-z0-9._%+-]+@[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$`
	Identity string `json:"identity"`

	// Environments are exact Environment names in this namespace.
	//
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:MaxLength=253
	// +kubebuilder:validation:items:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	// +listType=set
	Environments []string `json:"environments"`

	// Requesters name who may exchange.
	//
	// +kubebuilder:validation:Required
	Requesters KeyGrantRequesters `json:"requesters"`

	// Budget caps EACH exchange key this grant covers, through that key's own
	// "key:<id>" tag. When several grants cover one key the lowest maxBudget
	// applies. Removing it leaves the last ceiling in place.
	//
	// +optional
	Budget *BudgetBlock `json:"budget,omitempty"`
}

// KeyGrantStatus is the observed state of a KeyGrant.
type KeyGrantStatus struct {
	// ObservedGeneration is the metadata.generation the reconciler last processed.
	//
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Conditions carry Ready: False with reason IdentityUnknown (the identity
	// has never logged in to ACH) or EnvironmentMissing.
	//
	// +optional
	// +patchMergeKey=type
	// +patchStrategy=merge
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=kg
// +kubebuilder:printcolumn:name="Identity",type=string,JSONPath=".spec.identity"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// KeyGrant lets the named ServiceAccounts exchange their pod token for an ACH
// credential of Identity in the named Environments. Writing one is the power
// to act as Identity: the chart's keygrant-editor ClusterRole is bound to
// nobody and is never aggregated into admin or edit.
type KeyGrant struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   KeyGrantSpec   `json:"spec,omitempty"`
	Status KeyGrantStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// KeyGrantList contains a list of KeyGrant.
type KeyGrantList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []KeyGrant `json:"items"`
}

func init() {
	SchemeBuilder.Register(&KeyGrant{}, &KeyGrantList{})
}
```

`config/samples/ach_v1alpha1_keygrant.yaml`:

```yaml
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  labels:
    app.kubernetes.io/name: ach
    app.kubernetes.io/managed-by: kustomize
  name: ai-run-k8s-doctor
  namespace: ach-system
spec:
  identity: ai.run@ackstorm.com
  environments: [k8s-doctor]
  requesters:
    serviceAccounts:
      - ach-agent/agent-k8s-doctor
  budget:
    maxBudget: 50
    budgetDuration: 30d
```

Add `- ach_v1alpha1_keygrant.yaml` to `config/samples/kustomization.yaml` before the `# +kubebuilder:scaffold` line.

- [ ] **Step 2: Generate deepcopy, CRD, RBAC, chart copy and API reference**

The reconciler's RBAC markers (Step 5) must exist before `gen-manifests` regenerates `config/rbac/role.yaml`; run this step after Step 5 too if you write the controller first.

Run: `LOG=$(mktemp); timeout 1200 ./scripts/dev.sh make gen-code gen-manifests helm-sync gen-crd-ref-docs > "$LOG" 2>&1; echo "EXIT=$?"; git status --short`
Expected: `EXIT=0`; new `config/crd/bases/ach.ackstorm.ai_keygrants.yaml` and `deploy/helm/ach/crd-sources/ach.ackstorm.ai_keygrants.yaml` (with `helm.sh/resource-policy: keep`); `zz_generated.deepcopy.go`, `docs/api-reference/ach.ackstorm.ai.md` modified.

- [ ] **Step 3: Write the failing envtests**

`internal/controller/ach/keygrant_controller_test.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package ach

import (
	"context"
	"fmt"
	"slices"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach/api/ach/v1alpha1"
	achdb "github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/litellm"
)

// keyGrantStoreFake is the in-memory KeyGrantStore the suite reconciler writes.
type keyGrantStoreFake struct {
	mu     sync.Mutex
	grants map[string]achdb.KeyGrantRow
	keys   map[string]achdb.EkKeyInfo
}

func newKeyGrantStoreFake() *keyGrantStoreFake {
	return &keyGrantStoreFake{grants: map[string]achdb.KeyGrantRow{}, keys: map[string]achdb.EkKeyInfo{}}
}

func (s *keyGrantStoreFake) UpsertKeyGrant(_ context.Context, row achdb.KeyGrantRow) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.grants[row.Namespace+"/"+row.Name] = row
	return nil
}

func (s *keyGrantStoreFake) DeleteKeyGrant(_ context.Context, ns, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.grants, ns+"/"+name)
	return nil
}

func (s *keyGrantStoreFake) ListKeyGrants(_ context.Context, ns string) ([]achdb.KeyGrantRow, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []achdb.KeyGrantRow
	for _, g := range s.grants {
		if g.Namespace == ns {
			out = append(out, g)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func (s *keyGrantStoreFake) ListExchangeKeys(context.Context) ([]achdb.EkKeyInfo, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []achdb.EkKeyInfo
	for _, k := range s.keys {
		if k.Status != "revoked" {
			out = append(out, k)
		}
	}
	return out, nil
}

func (s *keyGrantStoreFake) RevokeEnvironmentKey(_ context.Context, keyID string) (*achdb.EkKeyInfo, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k, ok := s.keys[keyID]
	if !ok || k.Status == "revoked" {
		return nil, nil
	}
	k.Status = "revoked"
	s.keys[keyID] = k
	return &k, nil
}

func (s *keyGrantStoreFake) grant(ns, name string) (achdb.KeyGrantRow, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.grants[ns+"/"+name]
	return g, ok
}

func (s *keyGrantStoreFake) addKey(k achdb.EkKeyInfo) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.keys[k.KeyID] = k
}

func (s *keyGrantStoreFake) status(keyID string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.keys[keyID].Status
}

// keyGrantLLMFake answers the identity lookup and records revokes and budgets.
type keyGrantLLMFake struct {
	*litellm.NoopClient
	mu      sync.Mutex
	users   map[string]bool
	revoked []string
	budgets map[string]litellm.TagBudget
}

func newKeyGrantLLMFake() *keyGrantLLMFake {
	return &keyGrantLLMFake{NoopClient: &litellm.NoopClient{Log: logr.Discard()},
		users: map[string]bool{}, budgets: map[string]litellm.TagBudget{}}
}

func (f *keyGrantLLMFake) UserInfoByEmail(_ context.Context, email string) (*litellm.UserInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.users[email] {
		return &litellm.UserInfo{UserID: email, UserEmail: email}, nil
	}
	return nil, litellm.ErrNotFound
}

func (f *keyGrantLLMFake) RevokeKey(_ context.Context, token string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.revoked = append(f.revoked, token)
	return nil
}

func (f *keyGrantLLMFake) UpsertTagBudget(_ context.Context, name string, b litellm.TagBudget) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.budgets[name] = b
	return nil
}

func (f *keyGrantLLMFake) knowUser(email string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.users[email] = true
}

func (f *keyGrantLLMFake) budget(tag string) (litellm.TagBudget, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	b, ok := f.budgets[tag]
	return b, ok
}

func (f *keyGrantLLMFake) wasRevoked(token string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Contains(f.revoked, token)
}

func newKeyGrant(name, identity string, envs, sas []string, budget *achv1alpha1.BudgetBlock) *achv1alpha1.KeyGrant {
	return &achv1alpha1.KeyGrant{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace},
		Spec: achv1alpha1.KeyGrantSpec{Identity: identity, Environments: envs,
			Requesters: achv1alpha1.KeyGrantRequesters{ServiceAccounts: sas}, Budget: budget},
	}
}

// touchKeyGrant bumps an annotation so the reconciler runs again.
func touchKeyGrant(t *testing.T, kg *achv1alpha1.KeyGrant) {
	t.Helper()
	var cur achv1alpha1.KeyGrant
	if err := k8sClient.Get(context.Background(), client.ObjectKeyFromObject(kg), &cur); err != nil {
		t.Fatal(err)
	}
	patch := client.MergeFrom(cur.DeepCopy())
	if cur.Annotations == nil {
		cur.Annotations = map[string]string{}
	}
	cur.Annotations["ach.ackstorm.ai/test-touch"] = time.Now().Format(time.RFC3339Nano)
	if err := k8sClient.Patch(context.Background(), &cur, patch); err != nil {
		t.Fatal(err)
	}
}

func TestKeyGrantAdmission(t *testing.T) {
	ctx := context.Background()
	valid := func(name string) *achv1alpha1.KeyGrant {
		return newKeyGrant(name, "ai.run@ackstorm.com", []string{"k8s-doctor"}, []string{"ach-agent/agent-k8s-doctor"}, nil)
	}
	ok := valid("kg-admission-ok")
	if err := k8sClient.Create(ctx, ok); err != nil {
		t.Fatalf("valid KeyGrant refused: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(context.Background(), ok) })

	cases := []struct {
		name   string
		mutate func(*achv1alpha1.KeyGrant)
	}{
		{"upper-case identity", func(k *achv1alpha1.KeyGrant) { k.Spec.Identity = "AI.Run@ackstorm.com" }},
		{"glob identity", func(k *achv1alpha1.KeyGrant) { k.Spec.Identity = "*@ackstorm.com" }},
		{"no environments", func(k *achv1alpha1.KeyGrant) { k.Spec.Environments = nil }},
		{"glob environment", func(k *achv1alpha1.KeyGrant) { k.Spec.Environments = []string{"k8s-*"} }},
		{"no service accounts", func(k *achv1alpha1.KeyGrant) { k.Spec.Requesters.ServiceAccounts = nil }},
		{"bare service account", func(k *achv1alpha1.KeyGrant) { k.Spec.Requesters.ServiceAccounts = []string{"agent-k8s-doctor"} }},
		{"glob service account", func(k *achv1alpha1.KeyGrant) { k.Spec.Requesters.ServiceAccounts = []string{"ach-agent/*"} }},
		{"negative budget", func(k *achv1alpha1.KeyGrant) { k.Spec.Budget = &achv1alpha1.BudgetBlock{MaxBudget: -1} }},
	}
	for i, tc := range cases {
		kg := valid(fmt.Sprintf("kg-admission-bad-%d", i))
		tc.mutate(kg)
		if err := k8sClient.Create(ctx, kg); err == nil {
			_ = k8sClient.Delete(context.Background(), kg)
			t.Errorf("%s: admitted", tc.name)
		}
	}
}

func TestKeyGrantReconciler_ProjectionStatusFinalizer(t *testing.T) {
	ctx := context.Background()
	kg := newKeyGrant("kg-status", "kg-status@ackstorm.com", []string{"kg-status-env"}, []string{"agents/kg-status"}, nil)
	if err := k8sClient.Create(ctx, kg); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(context.Background(), kg) })

	waitReady := func(status metav1.ConditionStatus, reason string) {
		t.Helper()
		if !Eventually(func() bool {
			var got achv1alpha1.KeyGrant
			if err := k8sClient.Get(ctx, client.ObjectKeyFromObject(kg), &got); err != nil {
				return false
			}
			c := apimeta.FindStatusCondition(got.Status.Conditions, "Ready")
			return c != nil && c.Status == status && c.Reason == reason && got.Status.ObservedGeneration == got.Generation
		}, 30*time.Second, 250*time.Millisecond) {
			t.Fatalf("Ready never became %s/%s", status, reason)
		}
	}

	waitReady(metav1.ConditionFalse, "IdentityUnknown")
	if row, ok := keyGrantStore.grant(WatchNamespace, "kg-status"); !ok || row.Identity != "kg-status@ackstorm.com" ||
		!slices.Equal(row.ServiceAccounts, []string{"agents/kg-status"}) {
		t.Fatalf("projection row = %+v (present=%v)", row, ok)
	}

	keyGrantLLM.knowUser("kg-status@ackstorm.com")
	touchKeyGrant(t, kg)
	waitReady(metav1.ConditionFalse, "EnvironmentMissing")

	env := &achv1alpha1.Environment{
		ObjectMeta: metav1.ObjectMeta{Name: "kg-status-env", Namespace: WatchNamespace},
		Spec:       achv1alpha1.EnvironmentSpec{AuthorizedTeams: []string{"default"}},
	}
	if err := k8sClient.Create(ctx, env); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(context.Background(), env) })
	touchKeyGrant(t, kg)
	waitReady(metav1.ConditionTrue, "Granted")

	if err := k8sClient.Delete(ctx, kg); err != nil {
		t.Fatal(err)
	}
	if !WaitForGone(ctx, &achv1alpha1.KeyGrant{ObjectMeta: kg.ObjectMeta}, 15*time.Second) {
		t.Fatal("KeyGrant not removed: finalizer stuck")
	}
	if _, ok := keyGrantStore.grant(WatchNamespace, "kg-status"); ok {
		t.Fatal("projection row survived the finalizer")
	}
}

func TestKeyGrantReconciler_RevokesUngrantedKeysAndAppliesLowestBudget(t *testing.T) {
	ctx := context.Background()
	const who = "sweep@ackstorm.com"
	keyGrantLLM.knowUser(who)
	g50 := newKeyGrant("kg-sweep-50", who, []string{"sweep-env"}, []string{"agents/sweep-a"},
		&achv1alpha1.BudgetBlock{MaxBudget: 50, BudgetDuration: "30d"})
	g20 := newKeyGrant("kg-sweep-20", who, []string{"sweep-env"}, []string{"agents/sweep-a"},
		&achv1alpha1.BudgetBlock{MaxBudget: 20, BudgetDuration: "7d"})
	for _, g := range []*achv1alpha1.KeyGrant{g50, g20} {
		if err := k8sClient.Create(ctx, g); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = k8sClient.Delete(context.Background(), g) })
	}
	// Seed keys only once both grants are projected, so no sweep sees key a uncovered.
	if !Eventually(func() bool {
		_, a := keyGrantStore.grant(WatchNamespace, "kg-sweep-50")
		_, b := keyGrantStore.grant(WatchNamespace, "kg-sweep-20")
		return a && b
	}, 30*time.Second, 250*time.Millisecond) {
		t.Fatal("grants never projected")
	}
	tokA, tokB := "lt-sweep-a", "lt-sweep-b"
	keyGrantStore.addKey(achdb.EkKeyInfo{KeyID: "ekid_sweepa", OwnerEmail: who, Environment: "sweep-env",
		Requester: "agents/sweep-a", LiteLLMToken: &tokA, Status: "active"})
	keyGrantStore.addKey(achdb.EkKeyInfo{KeyID: "ekid_sweepb", OwnerEmail: who, Environment: "sweep-env",
		Requester: "agents/sweep-b", LiteLLMToken: &tokB, Status: "active"})
	touchKeyGrant(t, g50)

	if !Eventually(func() bool {
		return keyGrantStore.status("ekid_sweepb") == "revoked" && keyGrantLLM.wasRevoked(tokB)
	}, 30*time.Second, 250*time.Millisecond) {
		t.Fatal("the key no grant allows was not revoked (LiteLLM first, then the row)")
	}
	if !Eventually(func() bool {
		b, ok := keyGrantLLM.budget(litellm.KeyBudgetTag("ekid_sweepa"))
		return ok && b.MaxBudget == 20 && b.BudgetDuration == "7d"
	}, 30*time.Second, 250*time.Millisecond) {
		t.Fatal("the covered key does not carry the lowest grant budget")
	}
	if keyGrantStore.status("ekid_sweepa") != "active" || keyGrantLLM.wasRevoked(tokA) {
		t.Fatal("a covered key was revoked")
	}

	if err := k8sClient.Delete(ctx, g20); err != nil {
		t.Fatal(err)
	}
	if !Eventually(func() bool {
		b, ok := keyGrantLLM.budget(litellm.KeyBudgetTag("ekid_sweepa"))
		return ok && b.MaxBudget == 50
	}, 30*time.Second, 250*time.Millisecond) {
		t.Fatal("removing the lower grant must fall back to the remaining grant's budget")
	}
	if err := k8sClient.Delete(ctx, g50); err != nil {
		t.Fatal(err)
	}
	if !Eventually(func() bool { return keyGrantStore.status("ekid_sweepa") == "revoked" }, 30*time.Second, 250*time.Millisecond) {
		t.Fatal("removing the last covering grant must revoke the key")
	}
}
```

In `internal/controller/ach/suite_test.go`: add to the `var (…)` block

```go
	keyGrantStore   *keyGrantStoreFake
	keyGrantLLM     *keyGrantLLMFake
```

and after the `BackendIdentityPolicyReconciler` registration:

```go
	keyGrantStore = newKeyGrantStoreFake()
	keyGrantLLM = newKeyGrantLLMFake()
	if err := (&KeyGrantReconciler{
		Client:    mgr.GetClient(),
		Scheme:    mgr.GetScheme(),
		LiteLLM:   keyGrantLLM,
		Namespace: WatchNamespace,
		Log:       logr.Discard(),
		Store:     keyGrantStore,
	}).SetupWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "SetupWithManager(KeyGrant): %v\n", err)
		return 1
	}
```

Append to `internal/operator/resync/runnable_test.go`:

```go
func TestResync_KeyGrantSwept(t *testing.T) {
	s := testScheme(t)
	kg := &achv1alpha1.KeyGrant{ObjectMeta: metav1.ObjectMeta{Namespace: "ach-system", Name: "ai-run"}}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(kg).Build()
	ch := make(chan event.GenericEvent, 4)
	r := &Resync{Client: c, Namespace: "ach-system", Interval: time.Hour, Log: logr.Discard(),
		Channels: Channels{KeyGrant: ch}}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Start(ctx) }()
	select {
	case ev := <-ch:
		if ev.Object.GetName() != "ai-run" {
			t.Fatalf("expected ai-run, got %q", ev.Object.GetName())
		}
	case <-time.After(time.Second):
		t.Fatal("no KeyGrant event on initial fire")
	}
	cancel()
	<-done
}
```

- [ ] **Step 4: Run them to verify they fail**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/operator/resync > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|unknown field' "$LOG"`
Expected: FAIL — `unknown field KeyGrant in struct literal of type Channels`.
(The controller package will not compile until Step 5; its envtest run is Step 7.)

- [ ] **Step 5: Write the reconciler and wire it**

`internal/controller/ach/finalizers.go` — add to the const block:

```go
	keyGrantFinalizer              = "keygrants.ach.ackstorm.ai/finalizer"
```

`internal/controller/ach/keygrant_controller.go`:

```go
// SPDX-License-Identifier: Apache-2.0

package ach

import (
	"context"
	"fmt"
	"strings"

	"github.com/go-logr/logr"
	"github.com/jackc/pgx/v5/pgxpool"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"

	achv1alpha1 "github.com/ackstorm/ach/api/ach/v1alpha1"
	achdb "github.com/ackstorm/ach/internal/db"
	"github.com/ackstorm/ach/internal/keygrant"
	"github.com/ackstorm/ach/internal/litellm"
)

// KeyGrantStore is the reconciler's Postgres seam: the key_grants projection
// and the exchange keys grants govern.
type KeyGrantStore interface {
	UpsertKeyGrant(ctx context.Context, row achdb.KeyGrantRow) error
	DeleteKeyGrant(ctx context.Context, ns, name string) error
	ListKeyGrants(ctx context.Context, ns string) ([]achdb.KeyGrantRow, error)
	ListExchangeKeys(ctx context.Context) ([]achdb.EkKeyInfo, error)
	RevokeEnvironmentKey(ctx context.Context, keyID string) (*achdb.EkKeyInfo, error)
}

type poolKeyGrantStore struct{ pool *pgxpool.Pool }

// NewKeyGrantStore is the production KeyGrantStore.
func NewKeyGrantStore(pool *pgxpool.Pool) KeyGrantStore { return poolKeyGrantStore{pool: pool} }

func (s poolKeyGrantStore) UpsertKeyGrant(ctx context.Context, row achdb.KeyGrantRow) error {
	return achdb.UpsertKeyGrant(ctx, s.pool, row)
}
func (s poolKeyGrantStore) DeleteKeyGrant(ctx context.Context, ns, name string) error {
	return achdb.DeleteKeyGrant(ctx, s.pool, ns, name)
}
func (s poolKeyGrantStore) ListKeyGrants(ctx context.Context, ns string) ([]achdb.KeyGrantRow, error) {
	return achdb.ListKeyGrants(ctx, s.pool, ns)
}
func (s poolKeyGrantStore) ListExchangeKeys(ctx context.Context) ([]achdb.EkKeyInfo, error) {
	return achdb.ListExchangeKeys(ctx, s.pool)
}
func (s poolKeyGrantStore) RevokeEnvironmentKey(ctx context.Context, keyID string) (*achdb.EkKeyInfo, error) {
	return achdb.RevokeEnvironmentKey(ctx, s.pool, keyID)
}

// KeyGrantReconciler projects KeyGrant CRs into key_grants and owns exchange
// key cleanup: after every projection change it revokes the exchange keys no
// grant allows any more (LiteLLM first) and writes the lowest covering
// spec.budget onto every other one's key:<id> tag. The platform API never
// revokes on grant removal: its resolvers stop accepting the JWT within the
// cache window, and this sweep — single leader, LiteLLM master key — revokes.
// A key minted by an exchange that raced a grant's removal is caught by the
// next sweep (the 5-minute resync re-runs every grant).
type KeyGrantReconciler struct {
	client.Client
	Scheme    *runtime.Scheme
	LiteLLM   litellm.Client
	Namespace string
	Log       logr.Logger
	// Store nil skips the projection and the sweep.
	Store        KeyGrantStore
	ResyncSource chan event.GenericEvent
}

// update on keygrants is what adding and removing the finalizer takes.
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=keygrants,verbs=get;list;watch;update
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=keygrants/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=keygrants/finalizers,verbs=update

func (r *KeyGrantReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var kg achv1alpha1.KeyGrant
	if err := r.Get(ctx, req.NamespacedName, &kg); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !kg.DeletionTimestamp.IsZero() {
		if !controllerutil.ContainsFinalizer(&kg, keyGrantFinalizer) {
			return ctrl.Result{}, nil
		}
		if r.Store != nil {
			if err := r.Store.DeleteKeyGrant(ctx, kg.Namespace, kg.Name); err != nil {
				return ctrl.Result{}, fmt.Errorf("delete key_grants row: %w", err)
			}
			if err := r.sweep(ctx); err != nil {
				return ctrl.Result{}, err
			}
		}
		controllerutil.RemoveFinalizer(&kg, keyGrantFinalizer)
		return ctrl.Result{}, r.Update(ctx, &kg)
	}
	if controllerutil.AddFinalizer(&kg, keyGrantFinalizer) {
		return ctrl.Result{}, r.Update(ctx, &kg)
	}

	if r.Store != nil {
		if err := r.Store.UpsertKeyGrant(ctx, keyGrantRow(&kg)); err != nil {
			return ctrl.Result{}, fmt.Errorf("upsert key_grants row: %w", err)
		}
		if err := r.sweep(ctx); err != nil {
			return ctrl.Result{}, err
		}
	}

	cond, err := r.readyCondition(ctx, &kg)
	if err != nil {
		return ctrl.Result{}, err
	}
	if cur := apimeta.FindStatusCondition(kg.Status.Conditions, "Ready"); cur != nil &&
		cur.Status == cond.Status && cur.Reason == cond.Reason && cur.Message == cond.Message &&
		kg.Status.ObservedGeneration == kg.Generation {
		return ctrl.Result{}, nil
	}
	setExternalRefCondition(&kg.Status.Conditions, "Ready", cond.Status, cond.Reason, cond.Message, kg.Generation)
	kg.Status.ObservedGeneration = kg.Generation
	desired := kg.Status
	return ctrl.Result{}, retryStatusUpdate(ctx, r.Client, &kg, func(fresh *achv1alpha1.KeyGrant) {
		fresh.Status = desired
	})
}

func keyGrantRow(kg *achv1alpha1.KeyGrant) achdb.KeyGrantRow {
	row := achdb.KeyGrantRow{
		Namespace: kg.Namespace, Name: kg.Name, Identity: kg.Spec.Identity,
		Environments: kg.Spec.Environments, ServiceAccounts: kg.Spec.Requesters.ServiceAccounts,
	}
	if b := kg.Spec.Budget; b != nil {
		maxBudget := b.MaxBudget
		row.MaxBudget = &maxBudget
		row.BudgetDuration = b.BudgetDuration
	}
	return row
}

// readyCondition reports whether an exchange for this grant can succeed: the
// identity must be known to LiteLLM (teams are enrolled at its first ACH
// login) and every named Environment must exist.
func (r *KeyGrantReconciler) readyCondition(ctx context.Context, kg *achv1alpha1.KeyGrant) (metav1.Condition, error) {
	info, err := r.LiteLLM.UserInfoByEmail(ctx, kg.Spec.Identity)
	if err != nil && !litellm.IsNotFound(err) {
		return metav1.Condition{}, fmt.Errorf("LiteLLM user lookup: %w", err)
	}
	if info == nil {
		return metav1.Condition{Status: metav1.ConditionFalse, Reason: "IdentityUnknown",
			Message: kg.Spec.Identity + " has never logged in to ACH"}, nil
	}
	var missing []string
	for _, name := range kg.Spec.Environments {
		var env achv1alpha1.Environment
		err := r.Get(ctx, types.NamespacedName{Namespace: kg.Namespace, Name: name}, &env)
		if apierrors.IsNotFound(err) {
			missing = append(missing, name)
			continue
		}
		if err != nil {
			return metav1.Condition{}, err
		}
	}
	if len(missing) > 0 {
		return metav1.Condition{Status: metav1.ConditionFalse, Reason: "EnvironmentMissing",
			Message: "no Environment named " + strings.Join(missing, ", ")}, nil
	}
	return metav1.Condition{Status: metav1.ConditionTrue, Reason: "Granted",
		Message: "identity known; every Environment exists"}, nil
}

// sweep revokes every exchange key no grant allows any more, LiteLLM first
// (a 404 means the key is already gone), and writes the lowest covering
// spec.budget onto every other one.
// ponytail: one UpsertTagBudget per covered key per KeyGrant reconcile; skip
// unchanged ceilings if LiteLLM load ever shows it.
func (r *KeyGrantReconciler) sweep(ctx context.Context) error {
	grants, err := r.Store.ListKeyGrants(ctx, r.Namespace)
	if err != nil {
		return fmt.Errorf("list key_grants: %w", err)
	}
	keys, err := r.Store.ListExchangeKeys(ctx)
	if err != nil {
		return fmt.Errorf("list exchange keys: %w", err)
	}
	revoke, budgets := keygrant.Plan(grants, keys)
	for _, k := range revoke {
		if k.LiteLLMToken != nil {
			if err := r.LiteLLM.RevokeKey(ctx, *k.LiteLLMToken); err != nil && !litellm.IsHTTPNotFound(err) {
				return fmt.Errorf("revoke exchange key %s: %w", k.KeyID, err)
			}
		}
		if _, err := r.Store.RevokeEnvironmentKey(ctx, k.KeyID); err != nil {
			return fmt.Errorf("revoke exchange key row %s: %w", k.KeyID, err)
		}
		if err := litellm.DeleteKeyBudget(ctx, r.LiteLLM, k.KeyID); err != nil {
			r.Log.Error(err, "delete key budget", "key_id", k.KeyID)
		}
		r.Log.Info("revoked exchange key no grant allows", "key_id", k.KeyID,
			"environment", k.Environment, "requester", k.Requester)
	}
	for keyID, b := range budgets {
		if err := r.LiteLLM.UpsertTagBudget(ctx, litellm.KeyBudgetTag(keyID), b); err != nil {
			return fmt.Errorf("budget for exchange key %s: %w", keyID, err)
		}
	}
	return nil
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *KeyGrantReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return setupWithResync(mgr, r, &achv1alpha1.KeyGrant{}, "ach-keygrant", r.ResyncSource)
}
```

`internal/operator/resync/runnable.go` — add `KeyGrant chan<- event.GenericEvent` to `Channels` (after `LiteLLMConnection`), and to `sweepAll`:

```go
	sweepKind(ctx, r, r.Channels.KeyGrant, &achv1alpha1.KeyGrantList{}, "keygrants",
		func(l *achv1alpha1.KeyGrantList) []client.Object { return toObjects(l.Items) })
```

`cmd/ach/cmd/operator.go` — with the other channels: `kgCh := make(chan event.GenericEvent, resyncSourceChanCap)`; after the `BackendIdentityPolicyReconciler` registration:

```go
	if err = (&achcontroller.KeyGrantReconciler{
		Client:       mgr.GetClient(),
		Scheme:       mgr.GetScheme(),
		LiteLLM:      realLiteLLM,
		Namespace:    watchNS,
		Log:          ctrl.Log.WithName("controller").WithName("KeyGrant"),
		Store:        achcontroller.NewKeyGrantStore(dbPool),
		ResyncSource: kgCh,
	}).SetupWithManager(mgr); err != nil {
		return fmt.Errorf("unable to create controller KeyGrant: %w", err)
	}
```

and `KeyGrant: kgCh,` in the `resync.Channels{…}` literal. Update the "seven ACH CR Kinds" comment above the channels to "every ACH CR Kind".

Re-run Step 2's generate command now that the RBAC markers exist; expect `config/rbac/role.yaml` to gain the `keygrants`, `keygrants/status`, `keygrants/finalizers` rules.

- [ ] **Step 6: Docs**

`AGENTS.md`:
- In the owned-CRDs list, change "`LiteLLMConnection`,\n`BackendIdentityPolicy` (`api/` is authoritative —" to "`LiteLLMConnection`,\n`BackendIdentityPolicy`, `KeyGrant` (`api/` is authoritative —".
- Append to the Key exchange paragraph (Task 4): "The operator projects `KeyGrant` CRs into `key_grants` (no NOTIFY: readers query it per resolve) and, after every grant change and on the 5-minute resync, revokes (LiteLLM first) each exchange key no grant allows and writes the lowest covering `spec.budget` onto every other one's `key:<id>` tag; dropping `spec.budget` keeps the last ceiling. `Ready=False` reasons: `IdentityUnknown` (LiteLLM `/user/info` has no such user: the identity must log in to ACH once), `EnvironmentMissing`."

`references/understanding.md`: in the operator row change "Reconciles the 9 live CRD kinds" to "Reconciles the 10 live CRD kinds".

`references/adding-a-cr-kind.md`: in the Archetypes table change the governance row's kinds to "`Environment`, `BackendIdentityPolicy`, `KeyGrant`" and add footnote 6 under **Footnotes**, referenced from the governance cells of rows 2 and 6:

```markdown
6. `KeyGrant` has a projection table but no NOTIFY: its readers (the exchange
   handler and the exchange-JWT resolvers) query `key_grants` per request,
   behind the 60-second resolver cache, and keep no in-memory copy. It has no
   admin inventory route yet.
```

- [ ] **Step 7: Run the tests**

Run: `LOG=$(mktemp); timeout 600 make test-unit-pkg PKG=./internal/operator/resync > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: `ok`, `EXIT=0`.

Run: `LOG=$(mktemp); timeout 1500 make test-envtest-pkg PKG=./internal/controller/ach FOCUS='KeyGrant|CELAdmission|BackendIdentityPolicy' > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)' "$LOG"`
Expected: `ok  github.com/ackstorm/ach/internal/controller/ach`, `EXIT=0`.

Run: `LOG=$(mktemp); timeout 900 ./scripts/dev.sh go build ./... > "$LOG" 2>&1; echo "EXIT=$?"; cat "$LOG"`
Expected: `EXIT=0`.

- [ ] **Step 8: Commit**

```bash
git add api/ach/v1alpha1/ config/crd/bases/ach.ackstorm.ai_keygrants.yaml config/rbac/role.yaml config/samples/ \
  deploy/helm/ach/crd-sources/ach.ackstorm.ai_keygrants.yaml docs/api-reference/ach.ackstorm.ai.md \
  internal/controller/ach/ internal/operator/resync/ cmd/ach/cmd/operator.go \
  AGENTS.md references/understanding.md references/adding-a-cr-kind.md
git commit -m "feat(operator): KeyGrant CRD, projection and exchange key cleanup"
```

---

### Task 6: Grant authoring RBAC and operator documentation

**Files:**
- Modify: `deploy/helm/ach/templates/operator-rbac.yaml:16-50` (keygrants rules)
- Create: `deploy/helm/ach/templates/keygrant-editor-role.yaml`
- Modify: `scripts/helm-render-check.sh` (check 8)
- Create: `docs/user-guide/key-exchange.md`; Modify: `mkdocs.yml:12-15`, `docs/getting-started/installation.md`
- Create: `examples/keygrant.yaml`; Modify: `examples/README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the `keygrants` resource (Task 5), `system:auth-delegator` binding and TTL value (Task 4).
- Produces: ClusterRole `ach-keygrant-editor` (release `ach`), operator ClusterRole rules for `keygrants`; render check 8.

- [ ] **Step 1: Write the failing render check**

In `scripts/helm-render-check.sh`, after check 7, add and change the final line to `echo "helm-render-check OK (8 topologies)"`:

```bash
# 8. KeyGrant authoring: writing one is acting as its identity. The editor
# role ships bound to nobody and never aggregates into admin/edit; nothing the
# chart renders aggregates keygrants into those roles; the operator can read
# grants and manage their finalizer and status.
editor="$(render --show-only templates/keygrant-editor-role.yaml)"
grep -q "name: ach-keygrant-editor" <<<"$editor" || fail "ach-keygrant-editor ClusterRole missing"
grep -q "kind: ClusterRoleBinding" <<<"$editor" && fail "ach-keygrant-editor must ship bound to nobody"
grep -q "aggregate-to" <<<"$editor" && fail "ach-keygrant-editor must not aggregate into a built-in role"
awk -v RS='\n---\n' '/aggregate-to-(admin|edit)/' <<<"$(render)" | grep -q keygrants \
  && fail "a role aggregated into admin/edit names keygrants"
grep -q "keygrants/finalizers" <<<"$(render --show-only templates/operator-rbac.yaml)" \
  || fail "operator RBAC lacks keygrants"
```

Run: `LOG=$(mktemp); timeout 600 make helm-render-check > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 3 "$LOG"`
Expected: FAIL — `helm template … --show-only templates/keygrant-editor-role.yaml` exits non-zero (template missing), non-zero `EXIT`.

- [ ] **Step 2: Add the RBAC**

`deploy/helm/ach/templates/keygrant-editor-role.yaml`:

```yaml
---
# Writing a KeyGrant is the power to act as its identity: POST
# /platform/keys/exchange mints that identity's credential for the named
# ServiceAccounts. This role is bound to NOBODY by default — bind it to the
# platform-admin group only. It carries no aggregate-to-* label, so the
# built-in admin and edit roles never include it; any wildcard role (`*` on
# ach.ackstorm.ai or on all resources) in ACH's namespace still grants the
# same power.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ .Release.Name }}-keygrant-editor
  labels:
    {{- include "ach.commonLabels" . | nindent 4 }}
rules:
- apiGroups: ["ach.ackstorm.ai"]
  resources: ["keygrants"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["ach.ackstorm.ai"]
  resources: ["keygrants/status"]
  verbs: ["get"]
```

In `deploy/helm/ach/templates/operator-rbac.yaml`, append to the operator ClusterRole's `rules:` (after the `…/status` rule):

```yaml
# KeyGrant: read, finalizer and status only — the operator never authors a
# grant (writing one is acting as its identity). update is what adding and
# removing the finalizer takes.
- apiGroups: ["ach.ackstorm.ai"]
  resources: ["keygrants"]
  verbs: ["get", "list", "watch", "update"]
- apiGroups: ["ach.ackstorm.ai"]
  resources: ["keygrants/status"]
  verbs: ["get", "patch", "update"]
- apiGroups: ["ach.ackstorm.ai"]
  resources: ["keygrants/finalizers"]
  verbs: ["update"]
```

- [ ] **Step 3: Run the render check**

Run: `LOG=$(mktemp); timeout 600 make helm-render-check > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 3 "$LOG"`
Expected: `helm-render-check OK (8 topologies)`, `EXIT=0`.

- [ ] **Step 4: Write the operator-facing docs**

`docs/user-guide/key-exchange.md`:

````markdown
# Agent credentials by ServiceAccount token exchange

An agent running in ACH's cluster can obtain an Environment credential without
anyone minting an `ek_` by hand. Its pod proves who it is with its Kubernetes
ServiceAccount token; ACH answers with a short-lived ACH JWT that stands for one
stable `ek_` held inside ACH. The raw `ek_` never leaves ACH.

## Granting an identity to an agent

A platform admin writes one `KeyGrant` per agent in ACH's namespace:

```yaml
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-k8s-doctor
  namespace: ach
spec:
  identity: ai.run@ackstorm.com        # exactly one email, lower-case
  environments: [k8s-doctor]           # exact Environment names
  requesters:
    serviceAccounts:                   # exact "<namespace>/<name>"
      - ach-agent/agent-k8s-doctor
  budget:                              # optional, per key this grant covers
    maxBudget: 50
    budgetDuration: 30d
```

- No wildcards or patterns anywhere. A grant allows every combination of its
  `environments` and `serviceAccounts`; lists from different grants are never
  combined.
- The identity's teams must still intersect the Environment's
  `authorizedTeams`. Teams are enrolled at login, so the identity must log in
  to ACH once by hand before its first exchange; until then the grant reports
  `Ready=False` with reason `IdentityUnknown`. A missing Environment reports
  `EnvironmentMissing`.
- `spec.budget` caps each exchange key the grant covers, through the key's own
  `key:<id>` tag. When several grants cover one key the lowest `maxBudget`
  applies. Removing `spec.budget` leaves the last ceiling in place.
- Removing a grant entry cuts access within 60 seconds (the resolver cache) and
  the operator then revokes the key. Adding the entry back creates a new key:
  its spend history and per-key budget start over.

## Who may write grants

Writing a `KeyGrant` is the power to act as its identity. The chart ships the
ClusterRole `ach-keygrant-editor` (create, update, patch, delete on
`keygrants`) bound to nobody. Bind it to the platform-admin group only:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: platform-admins-keygrant-editor
  namespace: ach
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ach-keygrant-editor
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: Group
  name: platform-admins
```

The built-in `admin` and `edit` roles do not include `keygrants` writes, but any
wildcard role (`*` on `ach.ackstorm.ai` or on all resources) in ACH's namespace
grants them.

## The exchange

`POST <ACH_BASE_URL>/platform/keys/exchange` with
`Authorization: Bearer <pod-bound ServiceAccount token>` whose audience is
`ACH_BASE_URL` without a trailing slash, and the body
`{"identity": "ai.run@ackstorm.com", "environment": "k8s-doctor"}`.

`200`: `{"token": "<jwt>", "expires_in": 900, "key_id": "ekid_…"}`. Schedule
the next exchange from `expires_in`. The JWT is accepted wherever an `ek_` is
(`x-ach-key` or `Authorization: Bearer`) on the model, MCP, A2A, hydrate and
content routes. Every exchange for the same identity, Environment and
ServiceAccount returns the same `key_id`; a new JWT does not invalidate the
previous one, which stays valid until it expires.

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_argument` | body is not `{"identity","environment"}` |
| 401 | `invalid_token` | not a pod-bound ServiceAccount token for this ACH (wrong audience, a token minted without a bound pod, a `pk_`/`ek_`/ACH JWT) |
| 429 | `rate_limited` | too many exchange requests from this client or overall |
| 403 | `not_granted` | no single `KeyGrant` names this identity, Environment and ServiceAccount together |
| 404 | `environment_not_found` | no such Environment |
| 503 | `not_ready` | the Environment is not synced yet, or TokenReview is unavailable |
| 403 | `unauthorized_team` | the identity's teams do not intersect `authorizedTeams` |
| 409 | `key_suspended` | the exchange key was suspended by its owner |

The JWT's lifetime is `platformApi.keyExchange.tokenTTL` (default `15m`,
minimum `1m`).

## Installation requirements

- The chart binds the platform-api ServiceAccount to `system:auth-delegator`
  cluster-wide, which allows TokenReview and SubjectAccessReview only.
- The API server stores TokenReview bodies, which contain the token, only under
  audit level `Request` or `RequestResponse`. Set level `Metadata` for
  `tokenreviews` in the API server audit policy (on EKS, check the control-plane
  audit policy).
- Run agents in a namespace dedicated to them, where only Flux and the
  ach-agent operator can create pods: anyone who can create a pod there can run
  it as an agent's ServiceAccount.

## Audit

Every exchange, granted or denied, emits one `platform.ek.exchange` audit
event: actor (the ServiceAccount), requester, pod name and uid, identity,
Environment, key id, outcome and, for a denial, the reason. No token is ever
recorded.

## Residual exposure

A JWT is a bearer token valid until it expires; deleting the pod does not
revoke a JWT already issued. After a pod or grant is removed, exposure is
bounded by the JWT lifetime (15 minutes) and the resolver cache (60 seconds).
TokenReview itself accepts a deleted pod's token for up to 60 seconds.
````

`mkdocs.yml` — under `User Guide:` after the Editions entry: `      - Agent credentials by token exchange: user-guide/key-exchange.md`.

`docs/getting-started/installation.md` — append a section:

```markdown
## Agent credentials by token exchange

The platform API reviews ServiceAccount tokens for `POST /platform/keys/exchange`
through a cluster-wide `system:auth-delegator` binding the chart creates. Set
API server audit level `Metadata` for `tokenreviews`, and bind the
`ach-keygrant-editor` ClusterRole to platform admins only. See
[Agent credentials by token exchange](../user-guide/key-exchange.md).
```

`examples/keygrant.yaml`: the same YAML as `config/samples/ach_v1alpha1_keygrant.yaml` with `namespace: ach` and without the `labels:` block. In `examples/README.md` add a bullet to its examples list: "`keygrant.yaml` — a `KeyGrant` letting the `ach-agent/agent-k8s-doctor` ServiceAccount exchange its pod token for `ai.run@ackstorm.com`'s `k8s-doctor` credential (see `docs/user-guide/key-exchange.md`)."

`AGENTS.md`: append to the Key exchange paragraph: "Writing a `KeyGrant` is acting as its identity: the chart ships ClusterRole `<release>-keygrant-editor` bound to nobody and aggregates no ACH role into `admin`/`edit` (`make helm-render-check` check 8; e2e `kubectl auth can-i`)."

- [ ] **Step 5: Commit**

```bash
git add deploy/helm/ach/templates/keygrant-editor-role.yaml deploy/helm/ach/templates/operator-rbac.yaml \
  scripts/helm-render-check.sh docs/user-guide/key-exchange.md docs/getting-started/installation.md mkdocs.yml \
  examples/keygrant.yaml examples/README.md AGENTS.md
git commit -m "feat(chart): KeyGrant authoring RBAC and token exchange docs"
```

---

### Task 7: kind e2e — real pod token, real TokenReview

The 24x7 acceptance test (1-minute JWT, ≥3 rotations under continuous model, MCP and streaming load, zero failed requests) needs the ach-agent harness and lives in Plan B; ACH provides `platformApi.keyExchange.tokenTTL` for it. This task proves the ACH contract end to end.

**Files:**
- Create: `test/e2e/key_exchange_test.go`
- Modify: `test/e2e/README.md` (suite map row)

**Interfaces:**
- Consumes: the deployed chart (Tasks 4–6); e2e helpers `phase4SuiteGuard`, `phase4GatewayAuthority`, `oauthLogin`, `keysAPI`, `forwarderStatus`, `hydrateStatus`, `contentStatus`, `within`, `runCmd`, `runCmdLonger`, `runCmdStdin`, `phase3CapturePlatformAPILogs`, `phase3ParseAuditLines`, `namespace`, `ekStateUserEmail`, `demoPromptName`.

- [ ] **Step 1: Write the e2e test**

`test/e2e/key_exchange_test.go`:

```go
//go:build e2e

// SPDX-License-Identifier: Apache-2.0

// Key exchange e2e: a pod-bound ServiceAccount token reviewed by the kind API
// server buys an ACH JWT for kilgore's `demo` access through a KeyGrant; the
// JWT works wherever an ek_ does, and stops working once the grant is gone.
package e2e

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/ackstorm/ach/internal/audit"
)

const (
	kxNamespace = "ach-e2e-exchange"
	kxSA        = "agent-e2e"
	kxGrant     = "e2e-exchange"
)

func kxApply(t *testing.T, manifest string) {
	t.Helper()
	if out, err := runCmdStdin("kubectl apply -f -", manifest); err != nil {
		t.Fatalf("kubectl apply: %v\n%s", err, out)
	}
}

func kxSetup(t *testing.T) {
	t.Helper()
	kxApply(t, fmt.Sprintf(`apiVersion: v1
kind: Namespace
metadata:
  name: %[1]s
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: %[2]s
  namespace: %[1]s
---
apiVersion: v1
kind: Pod
metadata:
  name: %[2]s
  namespace: %[1]s
spec:
  serviceAccountName: %[2]s
  automountServiceAccountToken: false
  containers:
  - name: idle
    image: ach-mock:e2e
    imagePullPolicy: IfNotPresent
    args: ["a2a"]
    env:
    - name: MOCK_BIND_ADDRESS
      value: ":9090"
`, kxNamespace, kxSA))
	t.Cleanup(func() {
		_, _ = runCmd("kubectl", "delete", "keygrant", kxGrant, "-n", namespace, "--ignore-not-found", "--wait=false")
		_, _ = runCmd("kubectl", "delete", "rolebinding", "e2e-kx-admin", "e2e-kx-edit", "-n", namespace, "--ignore-not-found")
		_, _ = runCmd("kubectl", "delete", "namespace", kxNamespace, "--ignore-not-found", "--wait=false")
	})
	if out, err := runCmdLonger(150*time.Second, "kubectl", "wait", "--for=condition=Ready",
		"pod/"+kxSA, "-n", kxNamespace, "--timeout=120s"); err != nil {
		t.Fatalf("agent pod never Ready: %v\n%s", err, out)
	}
}

func kxToken(t *testing.T, audience string, podBound bool) string {
	t.Helper()
	args := []string{"create", "token", kxSA, "-n", kxNamespace, "--audience", audience, "--duration", "10m"}
	if podBound {
		args = append(args, "--bound-object-kind", "Pod", "--bound-object-name", kxSA)
	}
	out, err := runCmd("kubectl", args...)
	if err != nil {
		t.Fatalf("kubectl create token: %v\n%s", err, out)
	}
	return strings.TrimSpace(out)
}

func kxExchange(t *testing.T, base, bearer string) (int, map[string]any) {
	t.Helper()
	body := `{"identity":"` + ekStateUserEmail + `","environment":"demo"}`
	req, err := http.NewRequest(http.MethodPost, base+"/platform/keys/exchange", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+bearer)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("exchange: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

func kxErrCode(m map[string]any) string {
	e, _ := m["error"].(map[string]any)
	c, _ := e["code"].(string)
	return c
}

// kxForwarderBearer is forwarderStatus with the credential in Authorization.
func kxForwarderBearer(t *testing.T, base, token string) int {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, base+"/v1/chat/completions",
		strings.NewReader(`{"model":"demo-model","messages":[{"role":"user","content":"exchange e2e probe"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("forwarder: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func TestKeyExchange(t *testing.T) {
	phase4SuiteGuard(t)
	base := "http://" + phase4GatewayAuthority(t)
	audience := base                          // ACH_BASE_URL of the e2e release
	userJWT := oauthLogin(t, base, "").Access // kilgore logs in first: teams enrolled, grant can be Ready
	kxSetup(t)
	bound := kxToken(t, audience, true)

	t.Run("guards_and_unbound_tokens", func(t *testing.T) {
		for name, bearer := range map[string]string{
			"ek_ key":        "ek-" + strings.Repeat("0", 64),
			"user OAuth JWT": userJWT,
			"wrong audience": kxToken(t, "https://elsewhere.example", true),
			"unbound token":  kxToken(t, audience, false),
		} {
			if code, m := kxExchange(t, base, bearer); code != http.StatusUnauthorized || kxErrCode(m) != "invalid_token" {
				t.Errorf("%s: %d %v", name, code, m)
			}
		}
	})

	t.Run("no_grant", func(t *testing.T) {
		_, _ = runCmd("kubectl", "delete", "keygrant", kxGrant, "-n", namespace, "--ignore-not-found")
		if code, m := kxExchange(t, base, bound); code != http.StatusForbidden || kxErrCode(m) != "not_granted" {
			t.Fatalf("%d %v", code, m)
		}
	})

	kxApply(t, fmt.Sprintf(`apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: %s
  namespace: %s
spec:
  identity: %s
  environments: [demo]
  requesters:
    serviceAccounts: [%s/%s]
`, kxGrant, namespace, ekStateUserEmail, kxNamespace, kxSA))
	if out, err := runCmdLonger(150*time.Second, "kubectl", "wait", "--for=condition=Ready",
		"keygrant/"+kxGrant, "-n", namespace, "--timeout=120s"); err != nil {
		t.Fatalf("KeyGrant never Ready: %v\n%s", err, out)
	}

	var keyID, first string
	t.Run("exchange_and_reuse", func(t *testing.T) {
		code, m := kxExchange(t, base, bound)
		if code != http.StatusOK {
			t.Fatalf("%d %v", code, m)
		}
		keyID, _ = m["key_id"].(string)
		first, _ = m["token"].(string)
		if !strings.HasPrefix(keyID, "ekid_") || m["expires_in"] != float64(900) || first == "" {
			t.Fatalf("%v", m)
		}
		code, m = kxExchange(t, base, bound)
		if code != http.StatusOK || m["key_id"] != keyID || m["token"] == first {
			t.Fatalf("a second exchange must reuse %s with a new JWT: %d %v", keyID, code, m)
		}
		k := keysAPI{t: t, base: base, jwt: userJWT}
		if s, r := k.state(keyID); s != "active" {
			t.Fatalf("exchange key state %s %v", s, r)
		}
	})
	if keyID == "" {
		t.Fatal("no exchange key; later subtests depend on it")
	}

	t.Run("jwt_works_where_an_ek_does", func(t *testing.T) {
		if got := forwarderStatus(t, base, first); got != http.StatusOK {
			t.Errorf("forwarder x-ach-key: %d", got)
		}
		if got := kxForwarderBearer(t, base, first); got != http.StatusOK {
			t.Errorf("forwarder Authorization: %d", got)
		}
		if got := hydrateStatus(t, base, first, "demo"); got != http.StatusOK {
			t.Errorf("platform-api hydrate: %d", got)
		}
		if got := contentStatus(t, base, first, "demo", demoPromptName); got != http.StatusOK {
			t.Errorf("content-service: %d", got)
		}
	})

	t.Run("audit_without_credentials", func(t *testing.T) {
		logs := phase3CapturePlatformAPILogs(t, 3000)
		issued, denied := 0, 0
		for _, rec := range phase3ParseAuditLines(logs) {
			if rec.Action != audit.ActionEkExchange {
				continue
			}
			switch rec.Outcome {
			case audit.OutcomeIssued:
				issued++
			case audit.OutcomeNotGranted:
				denied++
			}
		}
		if issued < 2 || denied < 1 {
			t.Fatalf("platform.ek.exchange events: issued=%d not_granted=%d", issued, denied)
		}
		for _, secret := range []string{bound, first} {
			if strings.Contains(string(logs), secret) {
				t.Fatal("a credential reached the platform-api logs")
			}
		}
	})

	t.Run("admin_and_edit_cannot_write_keygrants", func(t *testing.T) {
		sa := "system:serviceaccount:" + kxNamespace + ":" + kxSA
		for _, role := range []string{"admin", "edit"} {
			rb := "e2e-kx-" + role
			if out, err := runCmd("kubectl", "create", "rolebinding", rb, "--clusterrole="+role,
				"--serviceaccount="+kxNamespace+":"+kxSA, "-n", namespace); err != nil {
				t.Fatalf("bind %s: %v\n%s", role, err, out)
			}
			out, _ := runCmd("kubectl", "auth", "can-i", "create", "keygrants.ach.ackstorm.ai", "-n", namespace, "--as="+sa)
			if strings.TrimSpace(out) != "no" {
				t.Errorf("%s may create KeyGrants: %q", role, out)
			}
			out, _ = runCmd("kubectl", "auth", "can-i", "create", "configmaps", "-n", namespace, "--as="+sa)
			if strings.TrimSpace(out) != "yes" {
				t.Errorf("control check: the %s binding is not effective: %q", role, out)
			}
			_, _ = runCmd("kubectl", "delete", "rolebinding", rb, "-n", namespace)
		}
	})

	t.Run("grant_removal_cuts_access_and_revokes", func(t *testing.T) {
		if out, err := runCmd("kubectl", "delete", "keygrant", kxGrant, "-n", namespace, "--timeout=60s"); err != nil {
			t.Fatalf("delete KeyGrant: %v\n%s", err, out)
		}
		within(t, 90*time.Second, "exchange JWT refused once its grant is gone", func() bool {
			return forwarderStatus(t, base, first) == http.StatusUnauthorized
		})
		k := keysAPI{t: t, base: base, jwt: userJWT}
		within(t, 60*time.Second, "operator revokes the exchange key", func() bool {
			s, _ := k.state(keyID)
			return s == "revoked"
		})
		if code, m := kxExchange(t, base, bound); code != http.StatusForbidden || kxErrCode(m) != "not_granted" {
			t.Fatalf("exchange after grant removal: %d %v", code, m)
		}
	})
}
```

Add a row to the suite map in `test/e2e/README.md`:

```markdown
| `key_exchange_test.go`                | `TestKeyExchange` — real pod-bound ServiceAccount token + real TokenReview: guards (ek_, OAuth JWT, wrong audience, unbound token → 401), not_granted, KeyGrant Ready, stable key reused across exchanges, the JWT on forwarder (x-ach-key + Authorization), hydrate and content-service, `platform.ek.exchange` audit without credentials, admin/edit cannot write keygrants, grant removal → 401 within the cache window and the key revoked |
```

- [ ] **Step 2: Compile the e2e package**

Run: `LOG=$(mktemp); timeout 900 ./scripts/dev.sh go vet -tags=e2e ./test/e2e/ > "$LOG" 2>&1; echo "EXIT=$?"; cat "$LOG"`
Expected: `EXIT=0`.

- [ ] **Step 3: Run the full e2e gate (once)**

Run it concurrently with the final code review of the branch, not after it. It keeps the cluster up afterwards (`make cluster-down` to reclaim).

Run: `LOG=$(mktemp); timeout 3000 make e2e-full > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^(ok|FAIL)|TestKeyExchange' "$LOG"`
Expected: `EXIT=0`, `--- PASS: TestKeyExchange` and its seven subtests, no `--- FAIL`. On a red `TestKeyExchange`, iterate on the kept cluster: `make cluster-sync` after a code fix, then `timeout 1200 make e2e-focus RUN='TestKeyExchange' > "$LOG" 2>&1`.

- [ ] **Step 4: Commit**

```bash
git add test/e2e/key_exchange_test.go test/e2e/README.md
git commit -m "test(e2e): token exchange with a real pod token and TokenReview"
```

---

### Task 8: Release prep — ACH 1.1.0

Convention (`references/release-pipeline.md`): the release is a commit whose subject is `chore(release): vX.Y.Z` at the head of `main`; `release.yml` bumps the manifests, tags and builds. Do NOT push, tag or run `make release-cut` (it pushes). The release commit is the branch's last commit, so a `git merge --ff-only` of this branch into `main` makes it the head commit the pipeline keys on.

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the pre-release gates**

`make qa-security` runs here because `go.mod` changed (Task 4).

Run, one at a time:
`LOG=$(mktemp); timeout 1200 make qa-lint > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 20 "$LOG"`
`LOG=$(mktemp); timeout 1800 make test-unit > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^FAIL' "$LOG"`
`LOG=$(mktemp); timeout 1800 make test-envtest-fast > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- FAIL|^FAIL' "$LOG"`
`LOG=$(mktemp); timeout 900 ./scripts/dev.sh make helm-sync-check > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 5 "$LOG"`
`LOG=$(mktemp); timeout 900 make qa-security > "$LOG" 2>&1; echo "EXIT=$?"; tail -n 20 "$LOG"`
`LOG=$(mktemp); timeout 1200 make test-integration-pkg PKG=./internal/db > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- (FAIL|SKIP)|^(ok|FAIL)' "$LOG"`

Expected: every `EXIT=0`; no `--- FAIL`, no `--- SKIP` in the integration run.

Check that the `<provider>-ach` skill needs no change (no `ach-cli` change in the range):

Run: `timeout 60 git diff "$(git describe --tags --abbrev=0)"..HEAD --stat -- cmd/ach-cli internal/cli internal/platformapi/opencode`
Expected: empty output.

- [ ] **Step 2: Write the CHANGELOG entry**

Insert after `## [Unreleased]` in `CHANGELOG.md`:

```markdown

## [1.1.0] - RELEASE_DATE

### Added
- `KeyGrant` (`ach.ackstorm.ai/v1alpha1`): a platform admin names one identity,
  its Environments and the ServiceAccounts (`<namespace>/<name>`) that may act
  as it, with an optional per-key budget. The operator projects it, reports
  `Ready` (`IdentityUnknown`, `EnvironmentMissing`), revokes exchange keys no
  grant allows any more and keeps each covered key's `key:<id>` budget at the
  lowest covering `spec.budget`.
- `POST /platform/keys/exchange`: a pod-bound ServiceAccount token plus a
  matching grant buys a short-lived ACH JWT (`aud: ach-ek`, 15 minutes,
  `platformApi.keyExchange.tokenTTL`) standing for one stable `ek_` per
  identity, Environment and ServiceAccount; the `ek_` never leaves ACH. The
  forwarder, content service and platform API accept the JWT wherever an `ek_`
  goes. Every exchange emits a `platform.ek.exchange` audit event.
- Helm: platform-api `system:auth-delegator` binding (TokenReview); ClusterRole
  `ach-keygrant-editor`, bound to nobody.

### Changed
- Exchange keys do not count against `max_keys`.
- Migration `000026` adds `environment_keys.issued_via` and `requester` and the
  `key_grants` table.
```

Then set the date:

Run: `sed -i "s/^## \[1.1.0\] - RELEASE_DATE$/## [1.1.0] - $(date -u +%F)/" CHANGELOG.md && grep -n '^## \[1.1.0\]' CHANGELOG.md`
Expected: `## [1.1.0] - <today's UTC date>`.

- [ ] **Step 3: Commit the release intent (no push, no tag)**

```bash
git add CHANGELOG.md
git commit -m "chore(release): v1.1.0"
```

Hand-off: after review, `git merge --ff-only` this branch into `main`; then `make pre-push && git push origin main` per `references/release-pipeline.md` ("Bundle the release intent with a real change"). Afterwards follow `AGENTS.md` "After every release — clean up".

---

## Self-review (against the spec)

**Spec coverage**

| Spec | Task |
|---|---|
| §4.1 stable key, `issued_via`/`requester`, partial unique index, race loser revokes + reads winner, no `max_keys`, name `exchange:<ns>/<sa>`, metadata `ach_issuer` + `ach_issued_via` + `ach_requester` | 1, 4 |
| §4.2 `KeyGrant` CRD, projection, exact-match validation, every-combination rule, no cross-grant mixing, `spec.budget` → `key:<id>` (lowest wins), `Ready` reasons via `/user/info` | 1, 5 |
| §4.2 who may write grants: editor role bound to nobody, no aggregation, RBAC test | 6 (render check), 7 (`kubectl auth can-i`) |
| §4.3 live authorization in the resolver; operator revokes with the LiteLLM-first path; operator RBAC + finalizer | 2, 5, 6 |
| §4.4 route outside Authn, bearer guard + rate limit before TokenReview, four checks in order, `not_granted` single code, JWT claims, response, factored mint, residual exposure documented | 3, 4, 6 |
| §4.5 resolver wraps OAuth resolver, accepted in `x-ach-key` and `Authorization` | 2, 7 |
| §4.6 audit action, no credentials; TokenReview `Metadata` audit level in install docs; derived audience; `CLAUDE.md` (`AGENTS.md`) auth-delegator line | 4, 6 |
| §6 ACH tests: check order matrix, stable key/race/no quota, live authorization, JWT cases + production order + both headers, guards + 429, audience slash, budget lowest, RBAC, audit, `ach_issuer` (orphan reaper keeps exchange keys: they are non-revoked `environment_keys` rows, so `ListManagedACHKeyIDs` already includes them — no reaper change), envtest, e2e | 1–7 |
| §6 acceptance (1-minute JWT, ≥3 rotations, zero failures) | setting in 4; test in Plan B (ach-agent harness) |
| §7.1 ACH 1.1.0 | 8 |

**Decisions this plan adds where the spec is silent**
- The exchange also writes the lowest covering budget at mint, so a fresh key is never uncapped until the next operator reconcile; the operator keeps it current.
- A suspended exchange key answers 409 `key_suspended` (a JWT for it could never authenticate).
- `key_grants` has no NOTIFY and no admin inventory route (documented in `references/adding-a-cr-kind.md`).
- Platform API runs TokenReview through the typed `authentication/v1` client; the route is unmounted outside a cluster.
- Per-client rate limit keys on the first `X-Forwarded-For` hop (forgeable); the global bucket is the hard bound.
- A key minted by an exchange that raced a grant's removal is unusable at once (resolver grant check) and revoked by the next sweep (≤5 minutes while any grant exists).

**Type consistency checked:** `db.KeyGrantRow`, `db.EkKeyInfo.Requester`, `db.EkInsertRow.IssuedVia/Requester`, `db.ExchangeTupleConstraint`, `keygrant.{Covering,Miss,Budget,Plan}`, `keystore.ExchangeAudience`, `keys.PeekAudience`, `jwt.Claims.JTI`, `(*jwt.Ed25519Signer).VerifyExp`, `createReq.{action,extra,requester,xdb}`, `(*createReq).mint` returning `(string, string, bool)`, `envkeys.TokenReviewFunc`, `envkeys.ExchangeDeps`, `platformapi.Deps.{TokenReview,KeyExchangeTTL}`, `ach.KeyGrantStore`, `resync.Channels.KeyGrant` — same names and signatures in every task that uses them.
