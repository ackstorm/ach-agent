# Provider selection by `model.type` (native gemini/anthropic wires)

**Date:** 2026-07-03 · **Status:** Shipped

## Symptom

A `type: gemini` model (`cron.yaml`: `model.name = gemini-flash-latest`, `model.type =
gemini`) failed every invocation:

```
[APIError] /chat/completions: Invalid model name passed in model=gemini-flash-latest.
metadata.url = http://127.0.0.1:<port>/v1/chat/completions
```

The generated `opencode.json` pointed at the `ach` custom provider
(`@ai-sdk/openai-compatible`) with baseURL `.../v1`, i.e. the **OpenAI chat/completions
wire** — regardless of `model.type`.

## Root cause (two bugs, both ignored `model.type`)

1. **`engine/lifecycle.py:write_opencode_config`** hardcoded ONE provider (`ach` /
   `@ai-sdk/openai-compatible`) for **every** type. A gemini model was forced through the
   OpenAI wire.
2. **`main.py`** derived the proxy path from the **hydration manifest endpoint**, not the
   type. ACH `/platform/hydrate` reports *every* model — including gemini — with
   `endpoint = https://ach.ackstorm.ai/v1`, so the override pinned the path to `/v1` even
   for `type: gemini`. The `model.type → prefix` map was computed then thrown away.

Net effect: `type` selected nothing. gemini round-tripped through chat/completions, which
also leaks gemini thought-signatures into `tool_call` ids (the original reason
`@ai-sdk/openai-compatible` was chosen as a workaround).

## Decision

`model.type` is **authoritative** for both the opencode provider and the proxy path. The
harness fronts each type on its **native wire**:

| `model.type` | opencode provider | npm | proxy path (baseURL) | model ref |
|--------------|-------------------|-----|----------------------|-----------|
| `openai` | custom `ach` | `@ai-sdk/openai-compatible` | `<proxy>/v1` | `ach/<name>` |
| `gemini` | built-in `google` | *(none — built-in)* | `<proxy>/gemini/v1beta` | `google/<name>` |
| `anthropic` | built-in `anthropic` | *(none — built-in)* | `<proxy>/anthropic` | `anthropic/<name>` |

The localhost model proxy is path-preserving (`target = {ach_base}{request.path}`) and
already routes `/gemini/*`, so `<proxy>/gemini/v1beta/models/<m>:generateContent` →
`ach.ackstorm.ai/gemini/v1beta/...` with `x-ach-key` injected. Verified against real ACH:
`gemini-flash-latest` on the native `/gemini/v1beta` wire returns a valid `candidates`
response (with `thoughtSignature`) — no chat/completions round-trip.

`npm` is written ONLY for the custom `ach` id (opencode honors `npm` only for non-builtin
ids); `google`/`anthropic` are opencode built-ins. The dummy `apiKey` never reaches ACH:
`x-goog-api-key` was added to the proxy's dropped request headers (symmetric with how the
openai path already drops `Authorization`).

## Changes

- `main.py`: `_MODEL_ENDPOINT_PREFIX["gemini"] = "gemini/v1beta"`; removed the
  manifest-endpoint override (kept `resolve_model` for membership validation); pass
  `model_type=cfg.model.type` to `EngineConfig`.
- `engine/lifecycle.py`: `EngineConfig.model_type` field; `_PROVIDER_BY_TYPE` map;
  `write_opencode_config` builds the provider block per type (npm only for `ach`).
- `engine/mcp_proxy.py`: drop `x-goog-api-key` from forwarded request headers.
- Test: `test_write_opencode_config_provider_by_model_type` locks the gemini + openai paths.

`model_type` defaults to `openai`, so existing configs / tests are unchanged.

## Note — the "phantom" hydration names (ACH-side, not the harness)

`/platform/hydrate` returns both `gemini.gemini-flash-latest` **and** bare
`gemini-flash-latest`, but litellm's `/v1/models` catalog only lists the dotted
`gemini.gemini-flash-latest`. On the openai wire the bare name 400s; on the native
`/gemini/v1beta` wire the bare name is the correct Gemini model id (proven above). The
duplicated/bare id in hydration is an ACH concern to reconcile separately — the harness fix
does not depend on it.
