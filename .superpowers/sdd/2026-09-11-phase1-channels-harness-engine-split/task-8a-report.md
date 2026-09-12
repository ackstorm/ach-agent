# Task 8A implementation report

## Scope delivered

- Added `ChannelSourceConfig`, a strict channels-role projection containing only
  source identity and transport blocks. `webhook-script` validates without carrying
  its harness-owned script. Source adapter annotations now consume this projection.
- Added `build_role_configs(cfg, split_mode=True)` in `ach_agent.boot.roles`.
  It returns `{"schemaVersion": "1", "channels": [...]}` and a credential-free
  `PublicEngineConfig` JSON projection. Split mode rejects `engine.forwardEnv`; local
  mode uses the existing `strip_forwarded_secrets` helper and sends only approved names.
  Typed MCP templates remain raw until E conversion.
- Extended `PublicEngineConfig` with engine identity/layout/persistence/public-context
  metadata and explicit engine environment names. Bootstrap metadata is excluded from
  native `EngineConfig` conversion. Trusted configured paths must be absolute and free
  of `..` components.
- Added split path helpers and public context links. H writes public hydrated context;
  E creates only its own home/workspace and links `.ach-state` and native skill paths.
  Existing baseline-managed hydration directories are preserved under `.pre-split`
  names before linking; native sessions and unrelated home files remain untouched.
- Added `run_engine(public_config)`: selected Pi/OpenCode driver, volatile or engine-home
  session map, existing `ExecutionService`/HTTP app, loopback health/metrics, lazy native
  launch, and nonzero failure after unreliable cleanup. `run_harness`/`run_channels` remain
  explicit Task 8B orchestration entrypoints and intentionally do not duplicate `main.py`.
- Added row-only startup session import (`SessionImportRequest`,
  `POST /execution/v1/session-import`, and `ExecutionClient.import_legacy_sessions`). It
  accepts no H database path, runs only before acquisition, persists the existing marker,
  and safely returns zero on repeat import without overwriting newer native mappings.
- Passthrough MCP env references resolve in the engine process; codemem availability is
  probed by E while H supplies only path/project. Native child env construction accepts
  explicit E-owned names and never copies H's environment wholesale.

## Task 8B interfaces

```python
build_role_configs(cfg: AgentConfig, *, split_mode: bool = True) \
    -> tuple[dict[str, JsonValue], dict[str, JsonValue]]
run_engine(public_config: JsonValue) -> None
ExecutionClient.import_legacy_sessions(
    rows: Iterable[SessionImportRow | Mapping[str, Any]]
) -> int
```

The first returned mapping has a `channels` list of `ChannelSourceConfig` JSON
objects. The second is `PublicEngineConfig` JSON with raw `mcpTemplates`, paths,
identity, persistence policy, public context, and no full `AgentConfig`, source
execution hooks, or managed credential values. Task 8B can call
`build_role_configs(cfg, split_mode=False)` for native local launch.

## Validation

Commands were run through the Docker devtools wrapper:

```text
rtk proxy ./scripts/dev.sh timeout -k 5 150 uv run pytest tests/test_task8a_roles.py -q
11 passed

rtk proxy ./scripts/dev.sh timeout -k 5 150 uv run pytest tests/test_task8a_roles.py tests/execution/test_service.py tests/execution/test_http.py -q
45 passed

rtk proxy ./scripts/dev.sh timeout -k 5 150 uv run pytest tests/config/test_schema.py tests/engine/test_context.py tests/engine/test_mcp_passthrough.py tests/execution/test_state.py tests/execution/test_wire.py tests/test_secret_forward_guard.py tests/test_session_store.py -q
109 passed

rtk proxy ./scripts/dev.sh timeout -k 5 150 uv run pytest tests/boot/test_engine_runner_http.py tests/execution/test_client.py tests/engine/test_codemem_opencode_config.py tests/integration/test_codemem_wiring.py -q
39 passed

rtk proxy ./scripts/dev.sh timeout -k 5 150 uv run pytest tests/test_main_wiring.py tests/config/test_schema.py tests/engine/test_mcp_passthrough.py tests/test_secret_forward_guard.py tests/test_task8a_roles.py -q
130 passed, 1 warning (existing Starlette/httpx deprecation)

rtk proxy ./scripts/dev.sh uv run ruff check <changed Task 8A files>
All checks passed

rtk proxy ./scripts/dev.sh uv run mypy <changed Task 8A files>
Success: no issues found

rtk proxy ./scripts/dev.sh uv run python scripts/gen_schema.py --check
OK: frozen schema matches AgentConfig
```

The plan's new `tests/test_split_roles.py`, `tests/test_split_hydration.py`, and
`tests/test_main_wiring.py` split-role cases are not present on this baseline; the
existing `tests/test_main_wiring.py` suite passed. Main/local orchestration, channel
startup, and native TUI remain Task 8B work.
