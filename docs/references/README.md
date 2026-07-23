# Decision records & design notes

Design/decision write-ups for significant changes (the *why*, not just the diff). One file per
decision, prefixed `YYYY-MM-DD-` so `ls` sorts chronologically. Add a row below when you add a doc.

| Date | Doc | Status | What it decides |
|------|-----|--------|-----------------|
| 2026-07-01 | [keyed-engine-pool](2026-07-01-keyed-engine-pool.md) | Shipped (`c57c92a`) | EnginePool keyed by `session_key`: one opencode server per key, per-key HOME isolation, `channel.session` reuse. |
| 2026-07-01 | [router-pool-vs-legacy](2026-07-01-router-pool-vs-legacy.md) | Decisions | Router / pool / session lifecycle vs legacy `ackbot-process`: what to keep vs port; splits into follow-up plans. |
| 2026-07-02 | [session-identity-and-bounds](2026-07-02-session-identity-and-bounds.md) | Accepted | `channel.session` becomes a block: separates `session_key` (lane/pool) from the oc-session reuse key; `maxTokens` + overflow bounds. |
| 2026-07-03 | [stats-observability](2026-07-03-stats-observability.md) | Shipped | Two-tier stats sink (Prometheus + redis stream), `v="1"` stream contract (`ach:sessions` + `ach:tools`), Tier-1 tool trace, OTel `gen_ai.*` alignment. |
| 2026-07-03 | [provider-by-model-type](2026-07-03-provider-by-model-type.md) | Shipped | `model.type` selects the opencode provider + native wire: gemini→built-in `google` on `/gemini/v1beta`, anthropic→built-in `anthropic`, openai→custom `ach`. Fixes `type: gemini` hitting `/v1/chat/completions`. |
| 2026-07-05 | [hindsight-memory-facade](2026-07-05-hindsight-memory-facade.md) | Shipped | Harness-hosted 4-tool MCP facade fronts Hindsight (bank_id + admin Bearer injected); boot-once provisioning of bank + mental models; `mentalModels` becomes rich specs; optional `auth`; per-repo via tags, not bank templating. |
| 2026-07-23 | [pi-engine-driver](2026-07-23-pi-engine-driver.md) | Shipped | Pi runs behind the symmetric EngineDriver seam over JSONL RPC; the harness owns terminal validation, engine-namespaced sessions, bounds, and loopback MCP egress via the vendored adapter. |
