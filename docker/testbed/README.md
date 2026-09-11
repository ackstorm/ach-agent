# ach-agent testbed — harness × real ach-memory

Runs the harness against a live `ach-memory` service so the memory path is exercised for
real: boot probe → `load_context` → the loopback facade → `retain`/`recall` from the agent.
Everything else in `docker/` mocks or skips memory.

## Layout

| | |
|---|---|
| memory stack | the cluster's `ach-memory`, reached through ACH's gateway. The direct route wants `ach-memory`'s own `docker-compose.yml` (postgres + hindsight + api) — **not owned here** |
| harness | `docker-compose.yaml` here, built from the repo root |
| contract | `config.yaml` — the only in-repo config with a populated `memory:` block. Names the hydrated server (`mcpServerId: ach-memory` + `auth: {type: ach}`), so the address comes from the ACH manifest that granted it. The direct route is `endpoint: http://api:8000/mcp/` + `auth.type: bearer` |
| checks | `check_memory.py` drives the harness; `check_agent.sh` drives the *agent* |
| identity | on the direct route, `bootstrap.sh` verifies the stack and writes the agent's identity into `./.env` (gitignored). Nothing is minted — see below. Through ACH the principal is the ek_ and there is nothing to write |

## Run it

Two routes. The gateway one is what `config.yaml` ships with, and the one production uses.

```bash
export ACH_TOKEN=ek-...        # the only credential either check needs

# 1. the harness checks — cold start, boot context, typed retain, containment, tool surface.
#    Reads the endpoint from the environment, NOT from config.yaml.
MEMORY_ENDPOINT=https://ach.ackstorm.ai/mcp/ach-memory uv run python check_memory.py

# 2. the agent checks — retain and recall across two sessions, then THE INVARIANT read out
#    of the untrusted process by the untrusted process. Costs three model turns.
./check_agent.sh

# 3. drive it by hand: the typed line is the prompt
docker compose run --rm agent
```

Direct, against a local ach-memory — still supported, and the only way to exercise the
`auth.type: bearer` arm:

```bash
# 1. memory stack. The Host allowlist is the one thing you must not omit (see "421" below);
#    the shipped default covers loopback only, and the harness arrives as `api:8000`.
cd ../../../ach-memory
MEMORY_MCP_ALLOWED_HOSTS='127.0.0.1,localhost,127.0.0.1:*,localhost:*,api:8000' \
HINDSIGHT_LLM_PROVIDER=mock HINDSIGHT_LLM_MODEL=mock-model \
HINDSIGHT_LLM_BASE_URL=http://127.0.0.1:9 HINDSIGHT_LLM_API_KEY=dummy \
docker compose up -d --build

# 2. verify the stack, the network and the allowlist; write ./.env
cd -
./bootstrap.sh                 # MEMORY_IDENTITY=<name> to be somebody else

# 3. the checks (no ACH_TOKEN needed — drives the harness code, not a model)
uv run python check_memory.py
```

`HINDSIGHT_LLM_PROVIDER=mock` means the memory service makes no real LLM call. The *agent*
still needs a real `ACH_TOKEN` on either route, and `config.yaml` + `docker-compose.yaml`
need putting back on the direct route.

## Identity is delegated — there is nothing to mint

ach-memory issues no credentials and stores none. Two providers resolve a caller, and they
read **different inputs**:

| Provider | Reads | Accepts |
|---|---|---|
| JWT | `Authorization: Bearer …` — but only when the token *looks like* a JWT, and that decision is final | a token the configured issuer signed |
| platform | whatever `MEMORY_AUTH_PLATFORM_INCOMING_HEADER` names (`x-litellm-api-key` in production, `authorization` on this stack) | anything its resolver can name |

On this compose stack the resolver is `deploy/dev-identity/whoami.py`, a sidecar that echoes
the bearer token back as the user id. **The token IS the identity**: `Bearer alice` is alice,
`Bearer alice+sre` is alice in the group `sre`, and two names are two people with two banks.
It authenticates everybody, on purpose, and must never run anywhere real.

That is why `bootstrap.sh` mints nothing: it writes the name you chose into `./.env`.

## What this catches that unit tests cannot

0. **Cold start.** An identity nobody has seen, naming a slug nobody has named, ends up with
   working memory unaided. The one check that must never be made to pass by a setup step.
1. **Boot context.** There is no probe: `load_context` answering IS reachability, and its
   text arrives as a `## Memory` block in the system prompt.
2. **Project derivation.** `POD_NAMESPACE=testbed` + `agent.name: memory-probe` ⇒ everything
   lands in bank `testbed-memory-probe`. Verify as that identity:
   `curl -H "Authorization: Bearer $(sed -n 's/^ACH_SECRET_MEMORY_ACHMEMORY=//p' .env)" localhost:8000/v1/projects`
3. **Containment.** Ask the agent to retain "into project `someone-else`". The facade
   *overrides* rather than fills, so it must still land in `testbed-memory-probe`.
4. **Tool surface.** The agent sees exactly five memory tools; `create_mental_model`,
   `forget`, `correct` and the rest are absent.
5. **Fail-open (D-02).** `docker stop <api container>` mid-session, then send another turn:
   the event completes with `"## Memory\n\nUnavailable…"` and `MEMORY_DEGRADED` increments —
   it does not abort.
6. **Durability across sessions, and THE INVARIANT.** `check_agent.sh` only: a claim retained
   in one session is recalled by the next, and the agent is asked to `cat` its own
   `opencode.json` and its own `/proc/self/environ` — the two files the threat model says it
   can read — with what comes back searched for the ek_. It reports counts and prints no
   capture: a leak test that dumps the config would put the ek_ in every log that ran it.
   Two proof-of-read gates come first, so a model that declines the commands fails rather
   than passing silently.

## The three real endpoints (measured 2026-09-09 against pro-ack-ai-platform)

`api.ackstorm.ai` and `ach.ackstorm.ai` are different gateways, and only one of them is ACH.

| URL | Routes to | Credential | Status |
|---|---|---|---|
| `https://ach.ackstorm.ai/mcp/<server-id>` | ACH's MCP gateway → LiteLLM → the server | `x-ach-key: <ek_>` → `auth: {type: ach}` | **works end to end** — validated against production with `ach-memory`: tools execute, `load_context` returns a real payload. Requires the ACH environment's access group (`ach-env-<name>`) to be listed on the LiteLLM MCP server, else every tool returns `"User not allowed to call this tool"` while `initialize` still answers 200 |
| `https://api.ackstorm.ai/memory/mcp/` | ach-memory direct (HTTPRoute `ach-memory`, PathPrefix `/memory`, stripped) | `auth: {type: bearer}` — production reads `x-litellm-api-key` (measured), so `header: x-litellm-api-key` + a LiteLLM key; the default `Authorization` needs the JWT provider, which is off there. The `mem_` keys this row was measured with no longer exist | route works; the credential shape changed under it |
| `https://api.ackstorm.ai/mcp/<anything>` | **LiteLLM**, via `api.ackstorm.ai`'s catch-all `/` route — never touches ACH | a LiteLLM virtual key (`sk-…`) | rejects an ek_: *"LiteLLM Virtual Key expected. Received=ek-…, expected to start with 'sk-'"* |

The middle row is why `endpoint` is taken verbatim: `/memory/mcp/` is exactly the shape that
breaks a client which appends its own `/mcp`.

**Identity differs by route, and that is the point.** Through ACH the principal comes from
LiteLLM's `/v2/user/info` (`MEMORY_AUTH_PLATFORM_USER_FIELD=user_id`) — an ACH service
account, so every agent under it shares one memory *user*. One bank per agent still holds,
but it holds because the facade pins `scope="project"` and injects `{namespace}-{agent.name}`
on every call: the agent cannot name another slug, and never reads or writes the shared user
bank. There is no service-side boundary between agents in one ACH account — the harness is
the boundary.

**Provisioning is ach-memory's job, and it does it.** `retain` is the one place allowed to
mint an unknown project, and it provisions the calling user's bank at the same time; every
read tool reports an absent project as its own empty shape rather than an error, so an
agent's first call — always a read — does not teach it that memory is broken. No bootstrap
step, on either route. `check_memory.py`'s check 0 is exactly this and must never be made to
pass by a setup step.

## Gotchas — all four of these were found by running this, not by reading code

**A project belongs to whoever first wrote to it.** A second identity naming the same slug
gets the absent-project empty shape, not a share — indistinguishable from never having
existed, which is deliberate: no read may be an existence oracle. So the agent's identity
must be *stable for the life of the agent*. Changing `MEMORY_IDENTITY` orphans the bank.

**Project creation is metered.** Ten per user per hour (`project_creation_limit`). That is
the guard that replaced `create=False`, and it is per *user* — `check_memory.py` uses a fresh
identity per run so a rerun never runs into it. Through ACH the principal is the ek_, so
reruns there do spend that budget.

**`recall` and `reflect` take `tags_filter`, not `tags`.** Renamed upstream while `retain`
kept `tags`. The facade maps the agent-facing `tags` onto the wire name in one place
(`ach_memory_facade.py`); a rename that slipped through would silently turn a narrowed
recall into an unfiltered one rather than erroring.

**421 on every MCP call.** The DNS-rebinding guard matches the `Host` header *including the
port*, and the shipped allowlist covers loopback only — the harness arrives as `api:8000` and
is refused. `bootstrap.sh` reads the running container's env and refuses to proceed without
it.

**There is no health probe, deliberately.** `load_context` is the reachability test: it is the
call the invocation depends on, it runs at the same point in the sequence, and it cannot lie.
A `GET {endpoint}/health` could — the endpoint is now a complete MCP URL that may sit behind a
gateway where `/health` is not a route, and a 404 reads as healthy under `status < 500`. This
testbed proved the 421 case for exactly that reason: the service answered `/health` 200 while
every MCP call failed.

**`retain` is not read-your-writes.** Fact extraction is asynchronous; an immediate `recall`
legitimately misses. `check_memory.py` retries for 20s rather than asserting once.

**Network name.** `bootstrap.sh` writes `MEMORY_NETWORK` into `./.env`. Override it if your
memory stack runs under a non-default compose project name.
