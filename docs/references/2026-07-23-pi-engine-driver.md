# Pi engine through the EngineDriver seam

## Decision

ACH treats Pi as a second engine behind the shared `EngineDriver` protocol. The
harness owns the terminal contract, session-map namespacing, bounds, cancellation,
and observability shape; each engine owns its transport and native session handle.

Pi uses `pi --mode rpc` with strict LF-framed JSONL over stdin/stdout. This keeps
the integration aligned with Pi's headless interface and avoids introducing an
engine-specific HTTP/SSE server into the harness. Pi's streamed events are mapped
to the shared engine event vocabulary, while the terminal contract remains in
`engine/base/terminal.py`.

The pool's sessions map is namespaced by engine type. Opencode session IDs and Pi
session-file paths therefore cannot cross-contaminate a persisted home when an
agent changes engine type. Pi sessions remain durable through Pi's
`--session-dir`, and transport-level tool-call and cancellation aborts preserve
the existing finite bounds.

MCP egress is harness-controlled: Pi receives loopback facade and proxy URLs,
passthrough entries, and codemem through a generated `mcp.json`. The vendored,
pinned pi-mcp-adapter is referenced from `settings.json`; ACH credentials stay in
the harness and never enter Pi config files or the clean-slate subprocess
environment.
