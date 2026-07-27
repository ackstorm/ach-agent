# Anthropic Wire Support — High-Level Design

**Status:** Draft for review. No code written.
**Repos:** `ach-agent` (all of it). **`ach` needs no change** — see Decision D1.
**Related:** `2026-07-27-agent-identity-metrics.md` (independent; do not bundle the releases).

---

## 1. Problem

`model.type: anthropic` is an advertised configuration — it is a valid value in the
`agent-config-v1` schema, the ACH CRD accepts it, and `engine/pi/models_json.py` maps it to a
real Pi provider (`anthropic-messages`). It does not work.

Two failures, at different layers:

1. **Routing.** An agent configured with `model.type: anthropic` sends model traffic to a path
   ACH does not serve. Every model call 404s. This affects **every** `cost.source`, not just
   the cost-accounting ones.
2. **Cost accounting.** `cost.source=litellm_usage` hard-fails at boot for this wire. The
   restriction is legitimate but its stated justification is wrong, and the real reason is
   deeper than "a parser is missing".

The second is only reachable once the first is fixed.

## 2. Evidence

Everything below was measured against the live production cluster or read at `1b24371` /
`ach@579cc5c`. Nothing here is inferred.

### 2.1 The routing defect

```python
# src/ach_agent/main.py:93
_MODEL_ENDPOINT_PREFIX: dict[str, str] = {
    "openai": "v1",
    "gemini": "gemini/v1beta",
    "anthropic": "anthropic",
}
# src/ach_agent/main.py:1426-1427
prefix = _MODEL_ENDPOINT_PREFIX[cfg.model.type]
model_base_url = f"{model_proxy_base}/{prefix}"
```

So the engine is pointed at `<local-proxy>/anthropic`. The local proxy accepts that prefix
(`_MODEL_PREFIXES = ("/v1", "/gemini", "/anthropic")`, `mcp_proxy.py:43`) and `_forward`
reconstructs the upstream target as `{ach_base}{request.path}` — verbatim, no rewrite. The
request therefore arrives at ACH as `/anthropic/…`.

ACH does not route it. From `ach@579cc5c`, `internal/gateway/routes.go`:

```
/platform/  /content/  /v1/  /v2/  /gemini/  /mcp/  /a2a/  /.well-known/
```

and `grep -rn anthropic internal/forwarder/ internal/gateway/` returns nothing. Result: 404.

### 2.2 The correct path already exists

LiteLLM serves both spellings. Probed from inside the cluster against
`litellm.litellm.svc:4000`:

| Path | Response | Meaning |
|---|---|---|
| `/anthropic/v1/messages` | 401 | provider-passthrough route exists, wants auth |
| `/gemini/v1beta/models` | 401 | same shape — this is what `/gemini` targets |
| `/v1/messages` | 405 on GET | route exists, POST-only |

The house convention is the second one. The working Claude Code configuration against this
LiteLLM is:

```sh
export ANTHROPIC_BASE_URL="https://api.ackstorm.ai"                    # root, no /anthropic
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer sk-..."
export ANTHROPIC_MODEL="claude-opus-4-8"
```

The base URL is the **root**; the Anthropic client appends `/v1/messages` itself. That path
is already routed by ACH's `/v1/*` blind passthrough (`internal/forwarder/server.go:74`) and
already carries `x-litellm-api-key` (`headers.StripAndRewrite`).

**The plumbing is complete. Only the harness's prefix is wrong.**

### 2.3 The cost defect is not "a missing parser"

The pricing math is genuinely provider-agnostic — four normalized counters times four rates:

```python
# src/ach_agent/engine/cost.py:53-64
raw_billable = usage.prompt_tokens - usage.cached_read_tokens - usage.cache_creation_tokens
billable_input = max(raw_billable, 0)
cost = (billable_input * prices.input_cost_per_token
        + usage.cached_read_tokens * prices.cache_read_input_token_cost
        + usage.cache_creation_tokens * prices.cache_creation_input_token_cost
        + usage.completion_tokens * prices.output_cost_per_token)
```

That first line is **subtractive**: it assumes `prompt_tokens` *includes* the cached tokens,
which is the OpenAI convention. Anthropic's `input_tokens` **excludes** cache reads and cache
creation — they are reported as separate, non-overlapping counters. Feeding an Anthropic
parser into this formula subtracts them twice and **under-reports the bill**.

This is already anticipated in the module docstring (`cost.py:5-7`):

> Switching to additive (`billable_input = prompt_tokens`) for a wire is permitted only on
> recorded B.7 evidence, cited in the change (U2).

And there is precedent for per-wire divergence — `_usage_from_gemini` hard-codes
`cache_creation_tokens=0` with the note *"gemini implicit caching has no creation cost"*.

So the parser is ~10 lines, but the **billable-input convention has to stop being a constant
inside `compute_cost` and become a property of the wire**. That is the actual design work.

### 2.4 The boot guard is scoped wrong

```python
# src/ach_agent/engine/cost.py:184-196
def validate_cost_source(source: str, model_type: str) -> None:
    """...the forwarder serves no ``/anthropic`` route for this source, so an Anthropic
    usage configuration is a deliberate boot hard-fail."""
    if source == "litellm_usage" and model_type == "anthropic":
        raise ValueError(...)
```

Two problems. The justification names a routing gap, but routing is broken for **every**
`cost.source`, not only `litellm_usage` — so the guard rejects one combination of a
configuration that is broken in all of them, implying the others work. And once §2.2's fix
lands, the routing justification evaporates while the §2.3 reason remains.

### 2.5 Why this matters more than it looks

`engine.type: pi` writes its own price table, and ACH zeroes it
(`engine/pi/models_json.py:31`):

```python
"cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
```

With Pi, `cost.source=engine` (the default) yields a **structural $0**, not a drifting
estimate. `litellm_usage` is not an improvement there — it is the only source that produces a
number at all. Any move toward Pi makes this work load-bearing.

## 3. Decisions

**D1 — Fix the routing in `ach-agent` only. No change to `ach`.**
Adding an `/anthropic/` route to the ACH forwarder would create a second path to the same
upstream, and would touch the forwarder's auth path — the security boundary — with no E2E
backstop in CI (e2e is local-only). The one-line prefix fix reaches the same LiteLLM endpoint
through the route that already exists and is already exercised by every OpenAI-wire agent.
Rejected alternative: mirror `/gemini` with an `/anthropic` handler in the forwarder.

**D2 — Make the billable-input convention a per-wire property.**
`compute_cost` must stop hard-coding the subtractive rule. The parser knows its wire's
convention; the pricing function should receive a normalized `TokenUsage` in which
`prompt_tokens` already means "billable input before cache adjustment", or an explicit
convention flag. Either shape is acceptable; the constraint is that adding a fourth wire must
not require editing `compute_cost`.

**D3 — Re-scope the boot guard to the wire, not the source combination.**
After D1, a wire is either fully supported (routing + parser + convention) or it is not. The
guard should reflect that. If a wire has no parser, only `litellm_usage` is refused; if a wire
has no route, nothing works and the failure should surface as a routing error at first call,
not as a cost-source guard.

**D4 — Do not bundle this with the agent-identity work.**
Different subsystems, different risk profiles, different verification. Two harness releases.

## 4. Phases

Each phase is independently shippable and independently valuable.

### Phase 1 — Make the Anthropic wire work at all

Correct `_MODEL_ENDPOINT_PREFIX["anthropic"]` so the engine's client lands on
`/v1/messages`. Cost accounting untouched; `cost.source` stays at its default.

**Blocking unknown (see §5, Q1):** the correct value is `""` if the engine's Anthropic client
appends `/v1/messages` to the base URL, or `"v1"` if it appends only `/messages`. This must be
measured, not assumed, and it must be measured for **both** engines (Pi's
`anthropic-messages` provider and opencode's Anthropic provider) — if they disagree, the
prefix cannot be a single per-wire constant and becomes per-engine.

**Acceptance:** an agent with `model.type: anthropic` completes an invocation end-to-end
through ACH against a real Anthropic model, with `cost.source` at its default.

**Verification:** the token-attributed route must survive too — `tokenize_model_base_url`
inserts `/t/<token>` after the authority, so with an empty prefix the client's own
`/v1/messages` suffix lands on the `/t/{token}/{tail:.*}` route with `tail=v1/messages`,
forwarded to `{ach_base}/v1/messages`. Confirm this, not just the plain route.

### Phase 2 — Anthropic usage parsing

Add `_usage_from_anthropic` to `_USAGE_PARSERS` and implement D2. Field mapping:

| `TokenUsage` | Anthropic field |
|---|---|
| `prompt_tokens` | `usage.input_tokens` (**excludes** cache — see D2) |
| `completion_tokens` | `usage.output_tokens` |
| `cached_read_tokens` | `usage.cache_read_input_tokens` |
| `cache_creation_tokens` | `usage.cache_creation_input_tokens` |

Record the B.7 evidence the U2 rule requires: a captured streaming and non-streaming response
payload showing the counters do not overlap. Cite it in the change.

**Acceptance:** a priced Anthropic invocation reports a cost within a small tolerance of
LiteLLM's own figure for the same request. Compare against
`litellm_spend_metric_total` and the `x-litellm-response-cost` header for a non-streaming
call — three independent numbers that must agree.

### Phase 3 — Streaming usage on the Anthropic wire

OpenAI needs an injected opt-in (`stream_options.include_usage`, `cost.py:451`,
`mutate_request`). Gemini repeats cumulative `usageMetadata` and the final payload wins.
Anthropic does neither: it emits usage natively in SSE, but **split across two event types** —
input counts on `message_start`, output counts on `message_delta`. `UsageObserver` currently
assumes a single final payload carries the whole record.

**Acceptance:** a streaming Anthropic invocation reports the same cost as the equivalent
non-streaming one.

### Phase 4 — Retire the boot guard

Implement D3 and correct the docstring, which currently names a routing gap that Phase 1
removed.

**Acceptance:** `cost.source=litellm_usage` + `model.type=anthropic` boots and prices
correctly. An unsupported wire still fails fast, with an accurate message.

## 5. Open questions

**Q1 (blocks Phase 1).** Does the engine's Anthropic client append `/v1/messages` or
`/messages` to the configured base URL? Measure for Pi's `anthropic-messages` provider and for
opencode independently. This is a one-request experiment against a local proxy that logs the
inbound path.

**Q2 (blocks Phase 1 acceptance).** Does LiteLLM's `/v1/messages` handler accept a bare
`x-litellm-api-key`, or does it require the `Bearer ` prefix the house Claude Code config
uses? ACH writes the value bare on `/v1`; only `/mcp` gets a `Bearer ` prefix
(`ach@internal/forwarder/proxy/proxy.go`). If bare is rejected, this becomes a change in
**`ach`**, not here — the same shape as the existing `/gemini` special case, where LiteLLM's
Google passthrough only reads `x-goog-api-key` and the Director had to move the credential.
That would invert D1 and must be re-reviewed before proceeding.

**Q3 (Phase 2).** Are Anthropic's `cache_read_input_tokens` and
`cache_creation_input_tokens` billed at LiteLLM's `cache_read_input_token_cost` /
`cache_creation_input_token_cost`, or does LiteLLM fold them differently for this provider?
The existing fallback is `input_cost_per_token` when a cache rate is absent.

**Q4 — ANSWERED: not urgent.** No agent or profile declares `model.type: anthropic`. Checked
both the GitOps source (`workloads/agents/**`: 7 `openai`, 4 `gemini`, 0 `anthropic`) and the
live cluster (5 `openai`, 2 `gemini`). This is a latent gap in an advertised configuration,
not a live outage — plan it, do not rush it.

## 6. Risks

- **Under-billing is silent.** Getting D2 wrong produces a plausible number that is simply too
  low. The Phase 2 three-way comparison exists specifically because a single number is not
  self-validating.
- **Q2 could move the work to `ach`.** If the auth header needs the `Bearer ` prefix on
  `/v1/messages`, the fix touches the forwarder's Director — the security boundary, with
  local-only E2E. That is a different review and a different release.
- **No CI backstop.** `ach`'s e2e suite runs nowhere automatically. Any change that ends up in
  `ach` needs a deliberate local `make e2e-full`.

## 7. Out of scope

- Adding an `/anthropic/` passthrough route to the ACH forwarder (rejected, D1).
- The agent-identity metric labels and outbound headers — separate plan, separate release.
- `spec.cost` on the ACH CRD — already shipped in `ach` PR #168.
- LiteLLM per-agent spend tags — deferred phase, tracked with the FinOps plan.
