# Process-Authoritative Agent Identity and Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the harness the process-authoritative source of agent identity, attach that identity to every exposed Prometheus sample and every governed model/MCP/A2A/hydrate request, then release the change as `v0.10.1` through the repository-owned CI release flow.

**Architecture:** A small process-global identity module owns the validated `agent` and `environment` values and the one case-insensitive header replacement function. Hydration bootstraps its request from trusted config values and commits process identity only after the response validates; metrics use one registry wrapper at the ASGI exposition boundary, while the shared proxy and A2A client consume the same process identity without changing model, MCP, or tool call sites. The metrics wrapper copies collector-owned `Metric` objects and samples, with process identity winning label collisions.

**Tech Stack:** Python 3.12, `prometheus-client==0.25.0`, FastAPI/Starlette `TestClient`, aiohttp, httpx, a2a-sdk 1.x, pytest with `asyncio_mode=auto`, uv, GitHub Actions, GHCR.

## Global Constraints

- Start from clean `main` at `b5a5cf331c2cbc64137b0fa80890e442672197bb`.
- The harness process is authoritative. Client-supplied `x-ach-agent` and `x-ach-environment` headers never win, regardless of casing.
- Every governed outbound model, MCP, A2A, and hydrate request carries exactly one `x-ach-agent` and exactly one `x-ach-environment` identity header.
- The `ACH_MODEL_BASE_URL` / `ACH_MODEL_HEADER` direct model override follows the same always-identify policy.
- Every sample exposed by `/metrics/`, including `name[]`-restricted scrapes and non-`ach_agent_*` default-process samples, carries `agent` and `environment`.
- Prometheus implementation and tests must match `prometheus-client==0.25.0`: `Counter("thing_total", "doc")` exposes `thing_total`, `collect()` yields `Iterable[Metric]`, and the library's restricted registry is `RestrictedRegistry`, not `CollectorRegistry`.
- Never mutate `Metric` objects or sample-label mappings owned by a collector. Return fresh `Metric` snapshots, preserve all metric fields including `unit`, preserve all sample fields via `_replace`, and make process identity win collisions.
- Invalid hydration must not change the already-committed process identity or create an identity metric side effect.
- Update the frozen prose contract `docs/schemas/operator-contract.md`; no JSON-schema field changes are required.
- Release exactly `v0.10.1`. `v0.10.0` already exists and resolves to `aec85d1761cc9cb66e946ea6da4b5f8006e705c1`.
- CI creates the `v0.10.1` tag, GitHub release, and GHCR images from the empty `chore(release): v0.10.1` marker commit. Never create or push a local release tag.
- Out of scope: all changes in `../ach`, including its PodMonitor; cost CRD work; converting these identity headers into LiteLLM tags.

---

## File and Responsibility Map

| File | Responsibility in this change |
|---|---|
| `src/ach_agent/identity.py` | Own immutable process identity snapshots and case-insensitive identity-header replacement. |
| `src/ach_agent/http/metrics.py` | Copy and stamp collected Prometheus metrics; implement restricted sample-name filtering without mutating the source registry. |
| `src/ach_agent/http/app.py` | Mount `make_asgi_app` with the identity-stamping registry wrapper. |
| `src/ach_agent/engine/hydrate.py` | Send bootstrap identity on hydrate and commit process identity only after manifest validation. |
| `src/ach_agent/engine/metrics.py` | Remove the superseded `AGENT_INFO` gauge. |
| `src/ach_agent/engine/mcp_proxy.py` | Replace client identity and inject process identity at the common model/MCP forwarding choke point. |
| `src/ach_agent/engine/a2a_egress.py` | Add process identity to the real A2A httpx client. |
| `src/ach_agent/main.py` | Pass trusted config agent/environment into hydrate; no per-model/MCP/A2A call-site threading. |
| `tests/test_identity.py` | Identity state and case-insensitive header-replacement tests. |
| `tests/http/test_metrics.py` | Non-mutating `Metric` copy, collision, repeated-scrape, unit, counter-name, and restricted-registry tests. |
| `tests/http/test_app.py` | Real `create_app([cfg], handler)` raw and `name[]` `/metrics/` integration tests. |
| `tests/engine/test_hydrate.py` | Exact hydrate headers, successful commit, and invalid-response no-side-effect tests. |
| `tests/e2e/test_opencode_mcp_structured_e2e.py` | Update the hydrate signature in the existing hermetic guard. |
| `tests/engine/test_mcp_proxy.py` | MCP spoof stripping and authoritative identity injection. |
| `tests/engine/test_model_proxy.py` | Model spoof stripping plus direct-model-override identity coverage. |
| `tests/engine/test_a2a_egress.py` | Hermetic verification of headers passed to the real A2A client construction seam. |
| `docs/schemas/operator-contract.md` | Frozen contract for identity sources, all four outbound paths, stripping, and metric labels. |
| `docs/configuration.md` | Operator-facing identity, metrics, header, override, and series-discontinuity behavior. |
| `CHANGELOG.md` | `v0.10.1` user-visible changes and Prometheus series-identity warning. |
| `pyproject.toml`, `uv.lock` | Patch version `0.10.1` and synchronized project lock entry. |

---

## Phase 1: Process Identity Foundation

### Task 1: Add the process-authoritative identity module

**Files:**

- Create: `src/ach_agent/identity.py`
- Create: `tests/test_identity.py`

**Interfaces:**

- Produces: `ProcessIdentity(agent: str, environment: str)`.
- Produces: `configure(agent: str, environment: str) -> None`.
- Produces: `current() -> ProcessIdentity`.
- Produces: `with_identity_headers(headers: Mapping[str, str], identity: ProcessIdentity | None = None) -> dict[str, str]`.
- Produces: `reset_for_testing() -> None`, used only by test fixtures.
- Identity replacement removes every case variant of `x-ach-agent` and `x-ach-environment`, retains unrelated headers, then inserts the two canonical lower-case keys. The supplied/process identity wins.

- [ ] **Step 1: Write the exact failing identity tests**

Create `tests/test_identity.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ach_agent import identity
from ach_agent.identity import ProcessIdentity


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


def test_configure_and_current_return_immutable_snapshot() -> None:
    identity.configure("classifier", "platform")
    assert identity.current() == ProcessIdentity(agent="classifier", environment="platform")


def test_identity_headers_strip_case_insensitively_and_process_wins() -> None:
    identity.configure("classifier", "platform")
    headers = identity.with_identity_headers(
        {
            "Accept": "application/json",
            "X-Ach-Agent": "spoofed-agent",
            "x-ACH-environment": "spoofed-environment",
        }
    )
    assert headers == {
        "Accept": "application/json",
        "x-ach-agent": "classifier",
        "x-ach-environment": "platform",
    }


def test_explicit_bootstrap_identity_does_not_mutate_process_state() -> None:
    identity.configure("committed", "stable")
    headers = identity.with_identity_headers(
        {"x-ach-key": "ek-test"},
        ProcessIdentity(agent="hydrate-agent", environment="requested-environment"),
    )
    assert headers == {
        "x-ach-key": "ek-test",
        "x-ach-agent": "hydrate-agent",
        "x-ach-environment": "requested-environment",
    }
    assert identity.current() == ProcessIdentity(agent="committed", environment="stable")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
./scripts/dev.sh uv run pytest tests/test_identity.py -v
```

Expected: collection fails because `ach_agent.identity` does not exist.

- [ ] **Step 3: Implement the minimal process identity API**

Create `src/ach_agent/identity.py` with this behavior:

```python
# SPDX-License-Identifier: Apache-2.0
"""Process-authoritative agent identity for metrics and governed egress."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_IDENTITY_HEADER_NAMES = frozenset({"x-ach-agent", "x-ach-environment"})


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    agent: str
    environment: str


_current = ProcessIdentity(agent="", environment="")


def configure(agent: str, environment: str) -> None:
    """Commit validated identity for this harness process."""
    global _current
    _current = ProcessIdentity(agent=agent, environment=environment)


def current() -> ProcessIdentity:
    """Return the current immutable identity snapshot."""
    return _current


def with_identity_headers(
    headers: Mapping[str, str], identity: ProcessIdentity | None = None
) -> dict[str, str]:
    """Replace any caller identity with exactly one authoritative header pair."""
    source = current() if identity is None else identity
    result = {key: value for key, value in headers.items() if key.lower() not in _IDENTITY_HEADER_NAMES}
    result["x-ach-agent"] = source.agent
    result["x-ach-environment"] = source.environment
    return result


def reset_for_testing() -> None:
    """Clear module state between tests; production never calls this."""
    configure("", "")
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
./scripts/dev.sh uv run pytest tests/test_identity.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit the identity foundation**

```bash
git add src/ach_agent/identity.py tests/test_identity.py
git commit -m "feat(identity): add process-authoritative identity"
```

---

## Phase 2: Non-Mutating Metrics Stamping

### Task 2: Add the copying registry wrapper

**Files:**

- Create: `src/ach_agent/http/metrics.py`
- Create: `tests/http/test_metrics.py`

**Interfaces:**

- Consumes: `ach_agent.identity.current()` from Task 1.
- Produces: `IdentityRegistry(registry: Collector = REGISTRY, names: frozenset[str] | None = None)`.
- Produces: `collect() -> Iterable[Metric]`.
- Produces: `restricted_registry(names: Iterable[str]) -> IdentityRegistry` for Prometheus `name[]` handling.
- The wrapper performs its own sample-name filtering. It must not delegate to `CollectorRegistry.restricted_registry()`: in `prometheus-client==0.25.0` that returns `RestrictedRegistry`, whose `_restricted_metric()` rebuild omits `unit`.

- [ ] **Step 1: Write exact failing tests for names, copying, collisions, and restricted collection**

Create `tests/http/test_metrics.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Iterable

import prometheus_client
import pytest
from prometheus_client.core import Metric
from prometheus_client.registry import CollectorRegistry, RestrictedRegistry

from ach_agent import identity
from ach_agent.http.metrics import IdentityRegistry


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


def _samples(registry: IdentityRegistry) -> dict[str, object]:
    return {
        sample.name: sample
        for metric in registry.collect()
        for sample in metric.samples
    }


def test_counter_total_name_matches_prometheus_client_025() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("thing_total", "doc", registry=registry).inc()
    identity.configure("classifier", "platform")
    samples = _samples(IdentityRegistry(registry))
    assert "thing_total" in samples
    assert "thing_total_total" not in samples


def test_prometheus_025_registry_types_are_not_conflated() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("typed_total", "doc", registry=registry).inc()
    library_restricted = registry.restricted_registry(["typed_total"])
    wrapped_metrics = list(IdentityRegistry(registry).collect())

    assert isinstance(library_restricted, RestrictedRegistry)
    assert all(isinstance(metric, Metric) for metric in wrapped_metrics)


class SharedMetricCollector:
    def __init__(self) -> None:
        self.metric = Metric("shared", "shared documentation", "gauge", unit="seconds")
        self.metric.add_sample(
            "shared_seconds",
            {"agent": "collector-agent", "source": "collector"},
            3.0,
            timestamp=123.0,
        )

    def collect(self) -> Iterable[Metric]:
        yield self.metric


def test_process_identity_wins_collision_and_preserves_every_metric_field() -> None:
    collector = SharedMetricCollector()
    identity.configure("process-agent", "platform")
    copied = next(iter(IdentityRegistry(collector).collect()))

    assert copied is not collector.metric
    assert copied.name == collector.metric.name
    assert copied.documentation == collector.metric.documentation
    assert copied.type == collector.metric.type
    assert copied.unit == "seconds"
    assert copied.samples[0] == collector.metric.samples[0]._replace(
        labels={"agent": "process-agent", "source": "collector", "environment": "platform"}
    )
    assert collector.metric.samples[0].labels == {
        "agent": "collector-agent",
        "source": "collector",
    }


def test_repeated_scrapes_do_not_mutate_or_share_returned_metrics() -> None:
    collector = SharedMetricCollector()
    wrapper = IdentityRegistry(collector)

    identity.configure("first-agent", "first-environment")
    first = next(iter(wrapper.collect()))
    identity.configure("second-agent", "second-environment")
    second = next(iter(wrapper.collect()))

    assert first is not second
    assert first.samples is not second.samples
    assert first.samples[0].labels["agent"] == "first-agent"
    assert second.samples[0].labels["agent"] == "second-agent"
    assert collector.metric.samples[0].labels["agent"] == "collector-agent"


def test_restricted_registry_filters_sample_names_and_preserves_unit() -> None:
    collector = SharedMetricCollector()
    identity.configure("classifier", "platform")
    restricted = IdentityRegistry(collector).restricted_registry(["shared_seconds"])
    metrics = list(restricted.collect())

    assert len(metrics) == 1
    assert metrics[0].unit == "seconds"
    assert [sample.name for sample in metrics[0].samples] == ["shared_seconds"]
    assert metrics[0].samples[0].labels["agent"] == "classifier"


def test_restricted_registry_drops_unrequested_metrics() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("kept_total", "doc", registry=registry).inc()
    prometheus_client.Counter("dropped_total", "doc", registry=registry).inc()
    identity.configure("classifier", "platform")

    samples = _samples(IdentityRegistry(registry).restricted_registry(["kept_total"]))
    assert set(samples) == {"kept_total"}
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
./scripts/dev.sh uv run pytest tests/http/test_metrics.py -v
```

Expected: collection fails because `ach_agent.http.metrics` does not exist.

- [ ] **Step 3: Implement copied metric snapshots and local name filtering**

Create `src/ach_agent/http/metrics.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Prometheus exposition wrapper that stamps immutable process identity."""

from __future__ import annotations

from collections.abc import Iterable
from copy import copy

from prometheus_client import REGISTRY
from prometheus_client.core import Metric
from prometheus_client.registry import Collector

from ach_agent import identity


class IdentityRegistry:
    """Expose copied, identity-stamped snapshots from an underlying collector."""

    def __init__(
        self,
        registry: Collector = REGISTRY,
        names: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._names = names

    def collect(self) -> Iterable[Metric]:
        stamp = identity.current()
        process_labels = {"agent": stamp.agent, "environment": stamp.environment}
        for metric in self._registry.collect():
            samples = [
                sample._replace(labels={**sample.labels, **process_labels})
                for sample in metric.samples
                if self._names is None or sample.name in self._names
            ]
            if not samples:
                continue
            copied = copy(metric)
            copied.samples = samples
            yield copied

    def restricted_registry(self, names: Iterable[str]) -> IdentityRegistry:
        requested = frozenset(names)
        if self._names is not None:
            requested &= self._names
        return IdentityRegistry(self._registry, requested)
```

The shallow `copy(metric)` is intentional: it preserves every current and future `Metric` field, while replacing `samples` with a new list. Rebuilding each sample through its `_replace` method preserves `value`, `timestamp`, `exemplar`, and `native_histogram` from the six-field 0.25.0 `Sample` tuple.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
./scripts/dev.sh uv run pytest tests/http/test_metrics.py -v
```

Expected: `6 passed`.

- [ ] **Step 5: Run strict typing on the new modules**

```bash
./scripts/dev.sh uv run mypy --strict src/ach_agent/identity.py src/ach_agent/http/metrics.py
```

Expected: success with no issues.

- [ ] **Step 6: Commit the wrapper**

```bash
git add src/ach_agent/http/metrics.py tests/http/test_metrics.py
git commit -m "feat(metrics): copy and stamp collected metrics"
```

### Task 3: Mount the wrapper and test raw plus restricted HTTP scrapes

**Files:**

- Modify: `src/ach_agent/http/app.py:26,197-201`
- Modify: `tests/http/test_app.py:17-24,128-146`

**Interfaces:**

- Consumes: `IdentityRegistry` from Task 2.
- Existing construction remains exactly `create_app([cfg], handler)`; do not introduce `app_factory`.

- [ ] **Step 1: Add an autouse identity reset fixture and exact HTTP tests**

In `tests/http/test_app.py`, import `identity`, add this fixture beside the helpers, and replace the existing `test_metrics` with the two tests below:

```python
from ach_agent import identity


@pytest.fixture(autouse=True)
def _reset_process_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


def _metric_sample_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line and not line.startswith("#")]


def test_metrics_stamps_every_exposed_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")
    identity.configure("classifier", "platform")
    app = create_app([_make_channel_cfg()], FakeHandler())

    with TestClient(app) as client:
        response = client.get("/metrics/")

    assert response.status_code == 200
    samples = _metric_sample_lines(response.text)
    assert samples
    assert all('agent="classifier"' in line for line in samples)
    assert all('environment="platform"' in line for line in samples)


def test_metrics_name_restriction_remains_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")
    identity.configure("classifier", "platform")
    app = create_app([_make_channel_cfg()], FakeHandler())

    with TestClient(app) as client:
        response = client.get(
            "/metrics/",
            params=[("name[]", "ach_agent_engine_watchdog_kills_total")],
        )

    assert response.status_code == 200
    samples = _metric_sample_lines(response.text)
    assert samples
    assert {line.split("{", 1)[0] for line in samples} == {
        "ach_agent_engine_watchdog_kills_total"
    }
    assert all('agent="classifier"' in line for line in samples)
    assert all('environment="platform"' in line for line in samples)
```

- [ ] **Step 2: Run the two HTTP tests and verify RED**

```bash
./scripts/dev.sh uv run pytest \
  tests/http/test_app.py::test_metrics_stamps_every_exposed_sample \
  tests/http/test_app.py::test_metrics_name_restriction_remains_stamped -v
```

Expected: assertions fail because `make_asgi_app()` still uses the unwrapped default registry.

- [ ] **Step 3: Mount the identity registry once at the exposition boundary**

Add this import to `src/ach_agent/http/app.py`:

```python
from ach_agent.http.metrics import IdentityRegistry
```

Replace the metrics app construction with:

```python
    metrics_app = make_asgi_app(registry=IdentityRegistry())
```

Do not change individual metric definitions or observation call sites.

- [ ] **Step 4: Run the HTTP suite and verify GREEN**

```bash
./scripts/dev.sh uv run pytest tests/http/test_app.py tests/http/test_metrics.py -v
```

Expected: all tests pass, including both raw and `name[]` scrapes.

- [ ] **Step 5: Commit the exposition change**

```bash
git add src/ach_agent/http/app.py tests/http/test_app.py
git commit -m "feat(metrics): stamp every exposed sample with identity"
```

---

## Phase 3: Hydrate, Model, MCP, and A2A Identity Headers

### Task 4: Bootstrap and commit identity through validated hydration

**Files:**

- Modify: `src/ach_agent/engine/hydrate.py:11,81-91`
- Modify: `src/ach_agent/engine/metrics.py:38-42`
- Modify: `src/ach_agent/main.py:66,1319`
- Modify: `tests/engine/test_hydrate.py`
- Modify: `tests/e2e/test_opencode_mcp_structured_e2e.py:78-94`

**Interfaces:**

- Changes: `fetch_hydration_manifest(base_url: str, ek: str, request_identity: ProcessIdentity) -> HydrationManifest`.
- Changes: `hydrate(base_url: str, ek: str, agent_name: str, requested_environment: str) -> HydrationManifest`.
- Hydrate sends config-derived bootstrap identity, validates the manifest, then calls `identity.configure(agent_name, manifest.environment)`.
- Invalid network responses, HTTP errors, and Pydantic validation errors leave prior process identity unchanged.

- [ ] **Step 1: Replace gauge assertions with exact header and state tests**

In `tests/engine/test_hydrate.py`, remove the `REGISTRY` import, import `identity` and `ProcessIdentity`, add an autouse reset fixture, and replace `test_hydrate_sets_agent_info_only_after_required_manifest_validation` with:

```python
from ach_agent import identity
from ach_agent.identity import ProcessIdentity


@pytest.fixture(autouse=True)
def _reset_process_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


async def test_hydrate_sends_bootstrap_headers_then_commits_validated_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def valid_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        assert url == "https://ach.example.com/platform/hydrate"
        assert headers == {
            "x-ach-key": "ek-abc",
            "x-ach-agent": "classifier",
            "x-ach-environment": "requested-platform",
        }
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", valid_post)
    manifest = await hydrate(
        "https://ach.example.com",
        "ek-abc",
        agent_name="classifier",
        requested_environment="requested-platform",
    )

    assert manifest.environment == "platform"
    assert identity.current() == ProcessIdentity(agent="classifier", environment="platform")


async def test_invalid_hydration_has_no_identity_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity.configure("already-committed", "stable")
    before = identity.current()
    missing_environment = {key: value for key, value in SAMPLE.items() if key != "environment"}

    async def invalid_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        assert headers["x-ach-agent"] == "new-agent"
        assert headers["x-ach-environment"] == "requested-platform"
        return missing_environment

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", invalid_post)
    with pytest.raises(ValidationError, match="environment"):
        await hydrate(
            "https://ach.example.com",
            "ek-abc",
            agent_name="new-agent",
            requested_environment="requested-platform",
        )

    assert identity.current() == before
```

Change the call in `test_hydrate_parses_manifest` to exactly:

```python
    m = await hydrate(
        "https://ach.example.com",
        "ek-abc",
        agent_name="hydrate-unit-agent",
        requested_environment="platform",
    )
```

Change the invalid fetch call to exactly:

```python
        await fetch_hydration_manifest(
            "https://ach.example.com",
            "ek-abc",
            ProcessIdentity(agent="hydrate-unit-agent", environment="platform"),
        )
```

- [ ] **Step 2: Run hydrate tests and verify RED**

```bash
./scripts/dev.sh uv run pytest tests/engine/test_hydrate.py -v
```

Expected: failures show the old signature and absence of both identity headers.

- [ ] **Step 3: Implement bootstrap headers and post-validation commit**

In `src/ach_agent/engine/hydrate.py`:

```python
from ach_agent import identity
from ach_agent.identity import ProcessIdentity
```

Replace the fetch/hydrate functions with:

```python
async def fetch_hydration_manifest(
    base_url: str,
    ek: str,
    request_identity: ProcessIdentity,
) -> HydrationManifest:
    headers = identity.with_identity_headers({"x-ach-key": ek}, request_identity)
    data = await _post_hydrate(f"{base_url.rstrip('/')}/platform/hydrate", headers)
    return HydrationManifest.model_validate(data)


async def hydrate(
    base_url: str,
    ek: str,
    agent_name: str,
    requested_environment: str,
) -> HydrationManifest:
    request_identity = ProcessIdentity(agent=agent_name, environment=requested_environment)
    manifest = await fetch_hydration_manifest(base_url, ek, request_identity)
    identity.configure(agent_name, manifest.environment)
    return manifest
```

Delete `AGENT_INFO` from `src/ach_agent/engine/metrics.py` and its import from hydrate. Keep all three existing counters unchanged.

Change the main boot call to:

```python
        manifest = await hydrate(
            cfg.capability.ach.base_url,
            ek,
            cfg.agent.name,
            cfg.capability.ach.environment,
        )
```

Replace the e2e hydrate call with exactly:

```python
        manifest = await hydrate(
            "https://ach.example",
            "ek_guard_secret",
            agent_name="e2e-guard-agent",
            requested_environment="guard",
        )
```

Configure no identity outside hydrate.

- [ ] **Step 4: Run hydrate and existing e2e guard tests**

```bash
./scripts/dev.sh uv run pytest \
  tests/engine/test_hydrate.py \
  tests/e2e/test_opencode_mcp_structured_e2e.py::test_hydration_returns_model_and_mcp_server -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Prove the superseded gauge is gone**

```bash
! rg -n "AGENT_INFO|ach_agent_info" src tests
```

Expected: exit status 0 from `!`; no matches are printed.

- [ ] **Step 6: Commit hydration identity**

```bash
git add \
  src/ach_agent/engine/hydrate.py \
  src/ach_agent/engine/metrics.py \
  src/ach_agent/main.py \
  tests/engine/test_hydrate.py \
  tests/e2e/test_opencode_mcp_structured_e2e.py
git commit -m "feat(identity): establish identity through hydration"
```

### Task 5: Enforce identity on model and MCP proxy traffic

**Files:**

- Modify: `src/ach_agent/engine/mcp_proxy.py:29-36,97-119`
- Modify: `tests/engine/test_mcp_proxy.py`
- Modify: `tests/engine/test_model_proxy.py`

**Interfaces:**

- Consumes: `identity.with_identity_headers()` at `_forward`, the common model/MCP choke point.
- `_DROP_REQUEST_HEADERS` includes both identity names so spoofed casing is stripped before authoritative insertion.
- Both plain model routes and token-attributed `/t/{token}` routes already use `_forward`; no observation call sites change.
- The direct model override still uses `ModelProxy` with a different `auth_header`/`auth_value`; identity policy remains identical.

- [ ] **Step 1: Add an exact MCP spoofing test**

Add this test to `tests/engine/test_mcp_proxy.py`; the fake upstream captures only the two identity headers from `request.raw_headers`, preserving duplicate counts:

```python
from ach_agent import identity


async def test_mcp_proxy_replaces_case_variant_client_identity() -> None:
    seen_identity: list[list[tuple[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        decoded = [(key.decode().lower(), value.decode()) for key, value in request.raw_headers]
        seen_identity.append(
            [(key, value) for key, value in decoded if key in {"x-ach-agent", "x-ach-environment"}]
        )
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    upstream_runner = web.AppRunner(app)
    await upstream_runner.setup()
    site = web.TCPSite(upstream_runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    proxy = McpProxy()
    identity.configure("classifier", "platform")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=f"http://127.0.0.1:{port}")],
            ek="ek-mcp",
            exclude=set(),
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                urls["m1"],
                headers={
                    "X-Ach-Agent": "spoofed-agent",
                    "x-ACH-environment": "spoofed-environment",
                },
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        identity.reset_for_testing()

    assert seen_identity == [
        [("x-ach-agent", "classifier"), ("x-ach-environment", "platform")]
    ]
```

- [ ] **Step 2: Add the direct-model-override test**

Add to `tests/engine/test_model_proxy.py`:

```python
from ach_agent import identity


async def test_direct_model_override_auth_still_replaces_identity_headers() -> None:
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.headers))
        return web.json_response({"ok": True})

    runner, upstream_url = await _start_fake_ach_router(handler)
    identity.configure("classifier", "platform")
    try:
        base = await start_model_proxy(
            upstream_url,
            "Bearer provider-key",
            auth_header="Authorization",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/v1/chat/completions",
                headers={
                    "X-ACH-AGENT": "spoofed-agent",
                    "X-Ach-Environment": "spoofed-environment",
                },
                json={"model": "test-model"},
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await stop_model_proxies()
        await runner.cleanup()
        identity.reset_for_testing()

    lowered = {key.lower(): value for key, value in captured[0].items()}
    assert lowered["authorization"] == "Bearer provider-key"
    assert lowered["x-ach-agent"] == "classifier"
    assert lowered["x-ach-environment"] == "platform"
    assert sum(key.lower() == "x-ach-agent" for key in captured[0]) == 1
    assert sum(key.lower() == "x-ach-environment" for key in captured[0]) == 1
```

This is the hermetic equivalent of the `ACH_MODEL_BASE_URL` plus `ACH_MODEL_HEADER=Authorization` boot branch in `main.py:1380-1419`.

- [ ] **Step 3: Run both tests and verify RED**

```bash
./scripts/dev.sh uv run pytest \
  tests/engine/test_mcp_proxy.py::test_mcp_proxy_replaces_case_variant_client_identity \
  tests/engine/test_model_proxy.py::test_direct_model_override_auth_still_replaces_identity_headers -v
```

Expected: upstream sees spoofed identity or no identity.

- [ ] **Step 4: Inject authoritative identity in `_forward`**

Import `identity`, add `x-ach-agent` and `x-ach-environment` to `_DROP_REQUEST_HEADERS`, and replace header construction with:

```python
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    headers[auth_header] = auth_value
    headers = identity.with_identity_headers(headers)
```

Do not gate this on `label`; both `label="model"` and `label="mcp"` require the identity pair. Because filtering uses `key.lower()`, all client casing variants are removed before the canonical lower-case pair is inserted.

- [ ] **Step 5: Run complete proxy suites**

```bash
./scripts/dev.sh uv run pytest \
  tests/engine/test_mcp_proxy.py \
  tests/engine/test_model_proxy.py \
  tests/engine/test_cost_integration.py -v
```

Expected: all tests pass; streaming, cost observers, normal ACH auth, and direct override auth remain intact.

- [ ] **Step 6: Commit proxy enforcement**

```bash
git add \
  src/ach_agent/engine/mcp_proxy.py \
  tests/engine/test_mcp_proxy.py \
  tests/engine/test_model_proxy.py
git commit -m "feat(identity): enforce identity on model and MCP egress"
```

### Task 6: Add identity to the real A2A client seam

**Files:**

- Modify: `src/ach_agent/engine/a2a_egress.py:68-84`
- Modify: `tests/engine/test_a2a_egress.py`

**Interfaces:**

- Consumes: `identity.with_identity_headers()` when `A2AAgentClient._ensure_client()` constructs its real `httpx.AsyncClient`.
- Tool handlers and `build_a2a_tools()` signatures remain unchanged.

- [ ] **Step 1: Add a hermetic real-client-construction test**

Add to `tests/engine/test_a2a_egress.py`:

```python
import a2a.client
import httpx
import pytest

from ach_agent import identity
from ach_agent.engine.a2a_egress import A2AAgentClient


async def test_real_a2a_client_receives_process_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, headers: dict[str, str], timeout: httpx.Timeout) -> None:
            captured["headers"] = dict(headers)
            captured["timeout"] = timeout

        async def aclose(self) -> None:
            captured["closed"] = True

    class FakeClientConfig:
        def __init__(self, *, httpx_client: object, streaming: bool) -> None:
            captured["httpx_client"] = httpx_client
            captured["streaming"] = streaming

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        captured["agent"] = agent
        captured["client_config"] = client_config
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(a2a.client, "ClientConfig", FakeClientConfig)
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    identity.configure("classifier", "platform")
    client = A2AAgentClient("https://peer.example/a2a", api_key="ek-a2a")

    await client._ensure_client()
    await client.close()

    assert captured["headers"] == {
        "x-ach-key": "ek-a2a",
        "x-ach-agent": "classifier",
        "x-ach-environment": "platform",
    }
    assert captured["agent"] == "https://peer.example/a2a"
    assert captured["streaming"] is False
    assert captured["closed"] is True
    identity.reset_for_testing()
```

- [ ] **Step 2: Run the A2A test and verify RED**

```bash
./scripts/dev.sh uv run pytest \
  tests/engine/test_a2a_egress.py::test_real_a2a_client_receives_process_identity_headers -v
```

Expected: captured headers contain only `x-ach-key`.

- [ ] **Step 3: Apply process identity at `_ensure_client`**

Import `identity` and replace the local header creation with:

```python
            headers: dict[str, str] = {}
            if self._api_key:
                headers["x-ach-key"] = self._api_key
            headers = identity.with_identity_headers(headers)
```

- [ ] **Step 4: Run the full A2A suite**

```bash
./scripts/dev.sh uv run pytest tests/engine/test_a2a_egress.py tests/engine/test_a2a_facade.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit A2A identity**

```bash
git add src/ach_agent/engine/a2a_egress.py tests/engine/test_a2a_egress.py
git commit -m "feat(identity): attach identity to A2A egress"
```

---

## Phase 4: Frozen Contract and Operator Documentation

### Task 7: Update contract and configuration documentation

**Files:**

- Modify: `docs/schemas/operator-contract.md:395-425,499-504,620-634,699-709`
- Modify: `docs/configuration.md:7-22`

**Interfaces:**

- Documentation must describe the implemented behavior, not the removed `ach_agent_info` gauge.
- No edits to `docs/schemas/agent-config-v1.schema.json`; no field/type/default changes occur.

- [ ] **Step 1: Update the frozen hydration and egress contract**

Make these precise changes in `docs/schemas/operator-contract.md`:

1. In the governed environment table and the paragraph below it, define process identity as `agent.name` plus the requested `capability.ach.environment`; state that the validated hydrate response's required `environment` becomes the committed process environment for subsequent metrics and egress.
2. In Hydration step 1, state that `POST /platform/hydrate` carries `x-ach-key`, `x-ach-agent`, and `x-ach-environment`; the latter two come from trusted rendered config for this bootstrap request.
3. State that validation failure commits no new process identity.
4. In proxy startup and §9, state that model and MCP forwarding strips client-supplied identity header names case-insensitively and injects exactly one canonical `x-ach-agent` and `x-ach-environment`, with harness values winning.
5. State that outbound A2A uses the same pair in its harness-owned httpx client.
6. State explicitly that the `ACH_MODEL_BASE_URL` / `ACH_MODEL_HEADER` development override changes upstream/auth only and never disables identity injection.
7. In §4, expand the `/metrics` endpoint requirement: every exposed sample, including Python/process defaults and `name[]`-restricted scrapes, carries the committed process `agent` and `environment` labels.

- [ ] **Step 2: Replace the stale gauge documentation**

Replace `docs/configuration.md`'s `ach_agent_info` section with operator-facing text that says:

- Every sample exposed by `GET /metrics/` carries `agent` and `environment`; this is applied at exposition, so current and future metric families require no call-site labels.
- `agent` comes from rendered `agent.name`; `environment` comes from the successfully validated hydrate manifest, while the hydrate request itself uses `capability.ach.environment` as its requested environment.
- Model, MCP, A2A, and hydrate outbound requests receive both identity headers from the harness.
- Client-supplied model/MCP identity headers are removed case-insensitively; process identity wins.
- The direct model override retains both identity headers.
- Adding these labels changes Prometheus series identity, so range queries spanning rollout show a discontinuity between pre-`v0.10.1` and post-`v0.10.1` series.
- No PodMonitor relabeling or `group_left` join is required by this repository.

- [ ] **Step 3: Check contract wording and schema non-change**

```bash
rg -n "x-ach-agent|x-ach-environment|every exposed sample|name\[\]|case-insensitive|v0.10.1" \
  docs/schemas/operator-contract.md docs/configuration.md
git diff --exit-code -- docs/schemas/agent-config-v1.schema.json
```

Expected: the first command shows every required topic; the schema command prints nothing and exits 0.

- [ ] **Step 4: Build docs strictly**

```bash
make docs-build
```

Expected: strict MkDocs build passes.

- [ ] **Step 5: Commit contract and docs**

```bash
git add docs/schemas/operator-contract.md docs/configuration.md
git commit -m "docs(identity): freeze metrics and egress identity contract"
```

---

## Phase 5: Patch Release Preparation and Gates

### Task 8: Prepare `v0.10.1`

**Files:**

- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Confirm the existing release baseline**

```bash
test "$(git rev-list -n1 v0.10.0)" = "aec85d1761cc9cb66e946ea6da4b5f8006e705c1"
test "$(git show -s --format=%s v0.10.0^{})" = "chore(release): v0.10.0"
```

Expected: both commands exit 0. Do not create, move, or push `v0.10.0`.

- [ ] **Step 2: Add exact release notes under `[unreleased]`**

Under `## [unreleased]` in `CHANGELOG.md`, add:

```markdown
### Added

- Every Prometheus sample exposed by the harness now carries process-authoritative `agent` and
  `environment` labels, including restricted `name[]` scrapes. Model, MCP, outbound A2A, and
  hydrate requests now carry the matching `x-ach-agent` and `x-ach-environment` headers.

### Changed

- Client-provided identity headers are stripped case-insensitively before harness identity is
  injected. The direct model-upstream override follows the same policy.
- Prometheus series identity changes at this release because every exposed sample gains two
  labels; range queries spanning the rollout show the old and new series separately.

### Removed

- The unreleased `ach_agent_info` join metric is removed in favor of labels on every sample.
```

- [ ] **Step 3: Run the repository release bump and synchronize the lock**

```bash
make release-bump VERSION=0.10.1
uv lock
```

Expected: `pyproject.toml` and the `ach-agent` package entry in `uv.lock` both say `0.10.1`; the changelog content is now under `## [0.10.1] - 2026-07-27`.

- [ ] **Step 4: Verify the version diff is limited and exact**

```bash
rg -n '^version = "0\.10\.1"$|^## \[0\.10\.1\] - 2026-07-27$' \
  pyproject.toml uv.lock CHANGELOG.md
git diff --check
```

Expected: the project metadata, lock self-entry, and changelog section all match; diff check passes.

- [ ] **Step 5: Commit release preparation**

```bash
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore(release): prepare v0.10.1"
```

### Task 9: Run all local quality and hermetic route-evidence gates

**Files:** none.

- [ ] **Step 1: Run formatting, typing, unit, conformance, and secret gates**

```bash
make verify
```

Expected: lint/format/mypy, non-e2e pytest, conformance, gitleaks, and trufflehog all pass.

- [ ] **Step 2: Run the full e2e suite**

```bash
./scripts/dev.sh uv run pytest tests/e2e -v
```

Expected: all e2e tests pass.

- [ ] **Step 3: Re-run the exact hermetic header evidence as a release record**

```bash
./scripts/dev.sh uv run pytest \
  tests/engine/test_hydrate.py::test_hydrate_sends_bootstrap_headers_then_commits_validated_identity \
  tests/engine/test_mcp_proxy.py::test_mcp_proxy_replaces_case_variant_client_identity \
  tests/engine/test_model_proxy.py::test_direct_model_override_auth_still_replaces_identity_headers \
  tests/engine/test_a2a_egress.py::test_real_a2a_client_receives_process_identity_headers -vv
```

Expected: four passes provide hermetic evidence for hydrate, MCP, model/direct override, and A2A real construction seams.

- [ ] **Step 4: Verify the final implementation tree before the release marker**

```bash
git status --short
git log --oneline b5a5cf331c2cbc64137b0fa80890e442672197bb..HEAD
git diff --check b5a5cf331c2cbc64137b0fa80890e442672197bb..HEAD
! git tag --points-at HEAD | rg '^v0\.10\.1$'
```

Expected: clean status, the ordered implementation commits are present, diff check passes, and no local `v0.10.1` tag exists.

---

## Phase 6: CI-Owned Release and Production Verification

### Task 10: Create the empty marker, push, and verify release automation

**Files:** none; the marker commit is intentionally empty.

- [ ] **Step 1: Create the exact empty release marker**

```bash
git commit --allow-empty -m "chore(release): v0.10.1"
test "$(git show -s --format=%s HEAD)" = "chore(release): v0.10.1"
```

Expected: the marker is `HEAD` and contains no file changes.

- [ ] **Step 2: Push the marker as the head commit on `main`**

```bash
test "$(git branch --show-current)" = "main"
git push origin main
```

Expected: push succeeds. Do not run `git tag` or push a tag.

- [ ] **Step 3: Wait for the release workflow and verify its outputs**

```bash
ACH_RELEASE_RUN=$(gh run list \
  --workflow release.yml \
  --branch main \
  --event push \
  --limit 1 \
  --json databaseId \
  --jq '.[0].databaseId')
gh run watch "$ACH_RELEASE_RUN" --exit-status
gh run view "$ACH_RELEASE_RUN" --log
```

Expected: the `release` job succeeds, including multi-architecture image push, CI-created tag, and GitHub release.

- [ ] **Step 4: Verify tag, release, and image digest**

```bash
git fetch origin --tags
test "$(git rev-list -n1 v0.10.1)" = "$(git rev-parse HEAD)"
gh release view v0.10.1 --json tagName,name,url --jq '{tagName,name,url}'
docker buildx imagetools inspect ghcr.io/ackstorm/ach-agent:v0.10.1
ACH_AGENT_DIGEST=$(docker buildx imagetools inspect ghcr.io/ackstorm/ach-agent:v0.10.1 \
  | sed -n 's/^Digest:[[:space:]]*//p' \
  | head -1)
test -n "$ACH_AGENT_DIGEST"
printf '%s\n' "$ACH_AGENT_DIGEST"
```

Expected: `v0.10.1` resolves to the empty marker commit, the GitHub release exists, the image is multi-arch, and a non-empty `sha256:` digest is printed.

### Task 11: Roll out and verify production identity, metrics, and digest

**Files:** none.

This task operates only on already-authorized production resources. It does not edit `../ach`, PodMonitor resources, CRDs, or LiteLLM configuration.

- [ ] **Step 1: Restart the operator-owned agent Deployments and wait for rollout**

```bash
kubectl -n ach rollout restart deployment -l app.kubernetes.io/name=ach-agent
kubectl -n ach rollout status deployment -l app.kubernetes.io/name=ach-agent --timeout=10m
kubectl -n ach get pods -l app.kubernetes.io/name=ach-agent -o wide
```

Expected: every selected Deployment completes rollout and every replacement pod is Ready.

- [ ] **Step 2: Prove running containers use the released digest**

Use the `ACH_AGENT_DIGEST` captured in Task 10:

```bash
kubectl -n ach get pods -l app.kubernetes.io/name=ach-agent -o json \
  | jq -e --arg digest "$ACH_AGENT_DIGEST" '
      [
        .items[].status.containerStatuses[]
        | select(.image | startswith("ghcr.io/ackstorm/ach-agent:"))
        | .imageID
        | contains($digest)
      ]
      | length > 0 and all
    '
```

Expected: jq prints `true` and exits 0.

- [ ] **Step 3: Port-forward one released pod and verify every raw sample is stamped**

```bash
ACH_AGENT_POD=$(kubectl -n ach get pods \
  -l app.kubernetes.io/name=ach-agent \
  -o jsonpath='{.items[0].metadata.name}')
ACH_EXPECTED_AGENT=$(kubectl -n ach exec "$ACH_AGENT_POD" -- python -c \
  'import json, os; p=os.environ.get("ACH_CONFIG_PATH", "/etc/ach-agent/config.json"); print(json.load(open(p, encoding="utf-8"))["agent"]["name"])')
ACH_EXPECTED_ENVIRONMENT=$(kubectl -n ach exec "$ACH_AGENT_POD" -- python -c \
  'import json, os; p=os.environ.get("ACH_CONFIG_PATH", "/etc/ach-agent/config.json"); print(json.load(open(p, encoding="utf-8"))["capability"]["ach"]["environment"])')
test -n "$ACH_EXPECTED_AGENT"
test -n "$ACH_EXPECTED_ENVIRONMENT"
ACH_METRICS_FILE=$(mktemp)
kubectl -n ach port-forward "pod/$ACH_AGENT_POD" 18080:8080 >"$ACH_METRICS_FILE.port-forward" 2>&1 &
ACH_PORT_FORWARD_PID=$!
trap 'kill "$ACH_PORT_FORWARD_PID" 2>/dev/null || true' EXIT
sleep 2
curl -fsS http://127.0.0.1:18080/metrics/ >"$ACH_METRICS_FILE"
awk -v expected_agent="$ACH_EXPECTED_AGENT" \
    -v expected_environment="$ACH_EXPECTED_ENVIRONMENT" '
  /^#/ || /^[[:space:]]*$/ { next }
  index($0, "agent=\"" expected_agent "\"") == 0 ||
  index($0, "environment=\"" expected_environment "\"") == 0 {
    print "incorrectly stamped sample: " $0 > "/dev/stderr"
    failed = 1
  }
  END { exit failed }
' "$ACH_METRICS_FILE"
```

Expected: both expected values are non-empty, curl succeeds, and awk proves every sample carries the exact rendered agent/environment pair. Keep the port-forward running through Step 4.

- [ ] **Step 4: Verify a restricted `name[]` scrape**

```bash
curl -fsSG http://127.0.0.1:18080/metrics/ \
  --data-urlencode 'name[]=ach_agent_engine_watchdog_kills_total' \
  >"$ACH_METRICS_FILE.restricted"
awk -v expected_agent="$ACH_EXPECTED_AGENT" \
    -v expected_environment="$ACH_EXPECTED_ENVIRONMENT" '
  /^#/ || /^[[:space:]]*$/ { next }
  $0 !~ /^ach_agent_engine_watchdog_kills_total[{ ]/ {
    print "unexpected restricted sample: " $0 > "/dev/stderr"
    failed = 1
  }
  index($0, "agent=\"" expected_agent "\"") == 0 ||
  index($0, "environment=\"" expected_environment "\"") == 0 {
    print "incorrectly stamped restricted sample: " $0 > "/dev/stderr"
    failed = 1
  }
  END { exit failed }
' "$ACH_METRICS_FILE.restricted"
kill "$ACH_PORT_FORWARD_PID"
wait "$ACH_PORT_FORWARD_PID" 2>/dev/null || true
trap - EXIT
```

Expected: only the requested counter sample is present and it has both labels.

- [ ] **Step 5: Verify all harness-exported series through PromQL**

Run these exact queries in the production Prometheus expression browser or HTTP API:

```promql
count by (__name__, agent, environment) (
  {namespace="ach", container="ach-agent", __name__=~"ach_agent_.*|process_.*|python_.*"}
)
```

Expected: every returned group has non-empty `agent` and `environment`.

```promql
count(
  {namespace="ach", container="ach-agent", __name__=~"ach_agent_.*|process_.*|python_.*"}
  unless
  {namespace="ach", container="ach-agent", __name__=~"ach_agent_.*|process_.*|python_.*", agent=~".+", environment=~".+"}
)
```

Expected: `0`. This checks all harness-exported application, Python, and process series, rather than one selected turn counter.

- [ ] **Step 6: Record route-header evidence for the released commit**

If production Forwarder/A2A receiver request logs expose header names and non-secret values, record one real hydrate, model, MCP, and A2A request showing the same agent/environment pair and exactly one of each identity header. If those logs intentionally redact or omit headers, attach the Task 9 `-vv` hermetic test output and its released commit SHA instead:

```bash
git rev-parse v0.10.1^{}
./scripts/dev.sh uv run pytest \
  tests/engine/test_hydrate.py::test_hydrate_sends_bootstrap_headers_then_commits_validated_identity \
  tests/engine/test_mcp_proxy.py::test_mcp_proxy_replaces_case_variant_client_identity \
  tests/engine/test_model_proxy.py::test_direct_model_override_auth_still_replaces_identity_headers \
  tests/engine/test_a2a_egress.py::test_real_a2a_client_receives_process_identity_headers -vv
```

Expected: the SHA is the CI-tagged `v0.10.1` marker and all four route-evidence tests pass from that released tree.

---

## Dependency Graph and Execution Order

```text
Task 1  process identity + header replacement
  ├──> Task 2  copied Metric wrapper
  │      └──> Task 3  raw + name[] HTTP exposition
  └──> Task 4  hydrate bootstrap + validated identity commit
           ├──> Task 5  shared model/MCP enforcement
           └──> Task 6  A2A client enforcement

Tasks 3 + 5 + 6
  └──> Task 7  frozen contract + configuration docs
           └──> Task 8  v0.10.1 changelog/version/lock
                    └──> Task 9  full gates + hermetic evidence
                             └──> Task 10 empty marker + push + CI release
                                      └──> Task 11 rollout + digest + production verification
```

Tasks 5 and 6 are implementation-independent after Task 4, but execute them sequentially under `superpowers:executing-plans` so every commit is reviewed and green before the next task. All release and production tasks are strictly serial.

## Completion Evidence

The implementation is complete only when all of the following are recorded:

- Unit tests prove accurate Prometheus 0.25.0 sample names and `Metric` return types.
- Collector-owned metrics remain unchanged across collisions, repeated scrapes, and a collector that reuses the same `Metric` instance; copied metrics retain `unit` and sample metadata.
- `create_app([cfg], handler)` passes raw and `name[]` endpoint tests with every sample stamped.
- Hydrate, model, MCP, direct model override, and A2A route tests show exactly one authoritative identity pair and case-insensitive spoof removal where client headers exist.
- Invalid hydration leaves process identity unchanged and `ach_agent_info` is absent.
- Frozen contract and configuration docs state that every exposed sample is stamped and all four outbound paths receive both headers.
- `make verify`, all e2e tests, strict docs build, and diff checks pass.
- `CHANGELOG.md`, `pyproject.toml`, and `uv.lock` agree on `0.10.1`.
- The empty marker is the released/tagged commit; no local tag was created.
- GitHub release and multi-arch GHCR image exist; production pods run that exact digest.
- Raw scrape, restricted scrape, and all-series PromQL verification pass after rollout.
- Real receiver evidence or the four released-tree hermetic route tests are attached to the release verification record.
