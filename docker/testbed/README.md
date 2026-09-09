# ach-agent testbed — harness × real ach-memory

Runs the harness against a live `ach-memory` service so the memory path is exercised for
real: boot probe → `load_context` → the loopback facade → `retain`/`recall` from the agent.
Everything else in `docker/` mocks or skips memory.

## Layout

| | |
|---|---|
| memory stack | `ach-memory`'s own `docker-compose.yml` (postgres + hindsight + api) — **not owned here** |
| harness | `docker-compose.yaml` here, built from the repo root, joined to that stack's network |
| contract | `config.yaml` — the only in-repo config with a populated `memory:` block. Uses `auth.type: bearer` (direct); through ACH's gateway it would be `endpoint: https://api.ackstorm.ai/mcp/ach-memory` + `auth: {type: ach}` |
| key | `bootstrap.sh` mints an ach-memory user key into `./.env` (gitignored) |

## Run it

```bash
# 1. memory stack — note the Host allowlist, see "421" below
cd ../../../ach-memory
cp .env.example .env       # set MEMORY_MASTER_KEY + MEMORY_MASTER_KEY_HASH (sha256 of it)
MEMORY_MCP_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:8000,localhost,localhost:8000,api:8000 docker compose up -d --build

# 2. mint the key (verifies the stack, the network and the allowlist first)
cd -
./bootstrap.sh                 # verifies the stack, mints a key, bootstraps the project

# 3. the checks (no ACH_TOKEN needed — drives the harness code, not a model)
uv run python check_memory.py

# 4. the agent (needs a real ek_)
ACH_TOKEN=ek-... docker compose run --rm agent
```

The shipped `.env.example` sets `HINDSIGHT_LLM_PROVIDER=mock`, so no real LLM call is made
by the memory service itself. The *agent* still needs a real `ACH_TOKEN`.

## What this catches that unit tests cannot

1. **Boot probe + context.** `/health` answers → no degraded note, and `load_context`'s text
   arrives as a `## Memory` block in the system prompt.
2. **Project derivation.** `POD_NAMESPACE=testbed` + `agent.name: memory-probe` ⇒ everything
   lands in bank `testbed-memory-probe`. Verify with the master key:
   `curl -H "Authorization: Bearer $MEMORY_MASTER_KEY" localhost:8000/v1/projects`
3. **Containment.** Ask the agent to retain "into project `someone-else`". The facade
   *overrides* rather than fills, so it must still land in `testbed-memory-probe`.
4. **Tool surface.** The agent sees exactly five memory tools; `create_mental_model`,
   `forget`, `correct` and the rest are absent.
5. **Fail-open (D-02).** `docker stop <api container>` mid-session, then send another turn:
   the event completes with `"## Memory\n\nUnavailable…"` and `MEMORY_DEGRADED` increments —
   it does not abort.

## Gotchas — all four of these were found by running this, not by reading code

**The project must be bootstrapped, and nothing in the harness does it.** `retain` and
`load_context` resolve the project with `create=False` (ach-memory `retention.py:59`,
`read_context.py:113`); only `POST /v1/bootstrap` creates one. Against an un-bootstrapped
project every memory call returns `PROJECT_NOT_FOUND` — and because the harness is fail-open,
that surfaces as *memory silently never working*, not as an error. `bootstrap.sh` calls it.
**Open question for production:** ach-memory intends to make this internal/automatic; until
it does, someone (the harness at boot, or the operator) has to make that call.

**A project is owned by the user whose key bootstrapped it.** A second user asking for the
same slug gets `PROJECT_NOT_FOUND`, not a share. So the agent's ach-memory key must be
*stable for the life of the agent* — re-minting it orphans the whole bank. `bootstrap.sh`
therefore reuses the key already in `./.env`; pass `FRESH=1` to deliberately start over.

**The master key needs the `mem_` prefix.** `keys.KEY_PREFIX` is a total discriminator: a
key without it is rejected before the hash is ever compared, with
`"not a mem_ key and no external provider is enabled"` — which reads like a provider
misconfiguration rather than a malformed key.

**421 on every MCP call.** The DNS-rebinding guard matches the `Host` header *including the
port*, and the shipped allowlist has `localhost:8000` but not `127.0.0.1:8000` — so
`http://127.0.0.1:8000` fails while `http://localhost:8000` works, for the same service.
`bootstrap.sh` checks the running container's env and refuses to proceed.

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
