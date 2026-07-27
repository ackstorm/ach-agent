# Agent Identity in Metrics and Outbound Headers — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the harness itself the single source of agent identity — stamp `agent` and `environment` on every Prometheus sample it exposes, and send the same identity as outbound headers on model traffic.

**Architecture:** A process-wide identity holder (`agent` from config at boot, `environment` from a successful platform hydrate) feeds two consumers. Metrics get it through a `Collector` wrapper around the default registry that rewrites every sample's label set at scrape time — no per-metric or per-call-site changes, and future metrics are covered automatically. The model proxy reads the same holder and adds `x-ach-agent` / `x-ach-environment` to requests it forwards to ACH. The `ach_agent_info` gauge and the ACH chart's PodMonitor relabeling are both deleted: they are two competing partial answers to the problem this plan solves once.

**Tech Stack:** Python 3.12, `prometheus_client` 0.25.0, `aiohttp` (proxy), `pytest` (asyncio_mode=auto), `uv`, Docker-routed `make`.

---

## Why this approach

Three mechanisms were on the table. This is the reasoning, recorded so it is not relitigated:

| Mechanism | Verdict |
|---|---|
| PodMonitor relabeling (repo `ach`, `deploy/helm/ach/templates/podmonitor.yaml`) | **Rejected.** Kubernetes-only, needs Prometheus Operator CRDs, and **structurally cannot supply `environment`** — the environment is not a pod label; it is only known after hydrate resolves the `ek_`. |
| `ach_agent_info{agent,environment}` gauge (`73f3ce5`) | **Rejected.** Forces a `group_left` join into every dashboard query. When the join is missing it does not error — it silently returns a wrongly-aggregated number. |
| Harness stamps labels on its own samples | **Chosen.** The harness is the only component that knows both facts. Works identically in Kubernetes, Docker, CI, and on a laptop. Dashboard queries stay direct. |

The relabeling's one genuine advantage — new metrics are covered without anyone remembering to label them — is preserved here by wrapping the registry instead of editing each metric.

## Current state (verified at `1b24371`)

14 `ach_agent_*` metric families across three modules:

- `src/ach_agent/engine/metrics.py` — 3 unlabelled counters + `AGENT_INFO` gauge (to be deleted)
- `src/ach_agent/router/metrics.py` — 4 unlabelled counters, `CHANNEL_INBOUND` (`channel`, `type`), `MEMORY_DEGRADED`
- `src/ach_agent/stats/metrics.py` — 8 families, incremented through positional `.labels(...)` in `observe()` / `observe_tool()` (lines 44-60)

None of them carries `agent` or `environment` today, so there are no label collisions to resolve.

Exposition: `make_asgi_app()` mounted at `/metrics` in `src/ach_agent/http/app.py:200-201`.

Identity sources: `cfg.agent.name` (`config/schema.py:38,715`); `manifest.environment` from `hydrate()` (`engine/hydrate.py:87-90`), called once at `main.py:1319`.

Exposition internals confirmed by inspecting the running image: `make_asgi_app(registry: Collector = REGISTRY)` and `exposition._bake_output` calls `registry.restricted_registry(params['name[]'])` **only** when the scrape URL carries a `name[]` query parameter. The wrapper must therefore implement `restricted_registry` as well as `collect`. `Sample` is a namedtuple with six fields (`name, labels, value, timestamp, exemplar, native_histogram`), so samples must be rebuilt with `_replace` rather than a positional constructor, or a future field addition silently drops data.

## Known consequence

Adding labels changes series identity. Range queries spanning the upgrade will show a break between the old and new series. This is unavoidable under any of the three mechanisms and is accepted.

---

## Task 1: Identity holder

**Files:**
- Create: `src/ach_agent/engine/identity.py`
- Test: `tests/engine/test_identity.py`

**Step 1: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Process-wide agent identity (agent name at boot, environment after hydrate)."""

from __future__ import annotations

import pytest

from ach_agent.engine import identity


@pytest.fixture(autouse=True)
def _reset() -> None:
    identity.reset()


def test_identity_defaults_to_empty_strings() -> None:
    assert identity.current() == {"agent": "", "environment": ""}


def test_set_agent_and_environment() -> None:
    identity.set_agent("classifier")
    identity.set_environment("platform")
    assert identity.current() == {"agent": "classifier", "environment": "platform"}


def test_current_returns_a_copy() -> None:
    identity.set_agent("classifier")
    snapshot = identity.current()
    snapshot["agent"] = "mutated"
    assert identity.current()["agent"] == "classifier"
```

**Step 2: Run test to verify it fails**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.identity'`

**Step 3: Write minimal implementation**

```python
# SPDX-License-Identifier: Apache-2.0
"""Process-wide agent identity, stamped onto every Prometheus sample.

The harness is the only component that knows both facts: ``agent`` comes from
config at boot, ``environment`` only from a successful platform hydrate. A
scrape-side relabeling can never supply ``environment`` — it is not a pod label —
which is why identity is emitted from inside the process.

Both keys always exist. They start as empty strings rather than being absent so
that the label set of a series never changes mid-process: Prometheus would
otherwise see two distinct series for the same metric across the hydrate
boundary. The agent hard-fails when hydrate fails, so the window in which
``environment`` is empty is the few seconds of boot.
"""

from __future__ import annotations

_IDENTITY: dict[str, str] = {"agent": "", "environment": ""}


def set_agent(name: str) -> None:
    """Record the configured agent name (called once at boot)."""
    _IDENTITY["agent"] = name


def set_environment(environment: str) -> None:
    """Record the hydrated environment (called once after a successful hydrate)."""
    _IDENTITY["environment"] = environment


def current() -> dict[str, str]:
    """Snapshot of the identity labels. Returns a copy — callers may not mutate state."""
    return dict(_IDENTITY)


def reset() -> None:
    """Clear the identity. Test-support only; never called by the harness."""
    _IDENTITY.update(agent="", environment="")
```

**Step 4: Run test to verify it passes**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_identity.py -v`
Expected: PASS (3 passed)

**Step 5: Commit**

```bash
git add src/ach_agent/engine/identity.py tests/engine/test_identity.py
git commit -m "feat(metrics): add process-wide agent identity holder"
```

---

## Task 2: Registry wrapper that stamps identity on every sample

**Files:**
- Modify: `src/ach_agent/engine/identity.py`
- Test: `tests/engine/test_identity.py`

**Step 1: Write the failing test**

Append to `tests/engine/test_identity.py`:

```python
import prometheus_client
from prometheus_client import CollectorRegistry


def _samples(registry) -> dict[str, dict[str, str]]:
    """Map sample name -> labels, across every metric the registry yields."""
    out: dict[str, dict[str, str]] = {}
    for metric in registry.collect():
        for sample in metric.samples:
            out[sample.name] = dict(sample.labels)
    return out


def test_stamps_identity_on_unlabelled_metric() -> None:
    inner = CollectorRegistry()
    prometheus_client.Counter("thing_total", "doc", registry=inner).inc()
    identity.set_agent("classifier")
    identity.set_environment("platform")

    labels = _samples(identity.IdentityRegistry(inner))["thing_total_total"]
    assert labels == {"agent": "classifier", "environment": "platform"}


def test_preserves_existing_labels() -> None:
    inner = CollectorRegistry()
    prometheus_client.Counter("thing_total", "doc", ["model"], registry=inner).labels("gpt").inc()
    identity.set_agent("classifier")

    labels = _samples(identity.IdentityRegistry(inner))["thing_total_total"]
    assert labels == {"agent": "classifier", "environment": "", "model": "gpt"}


def test_stamps_histogram_buckets_and_created_series() -> None:
    inner = CollectorRegistry()
    prometheus_client.Histogram("dur_seconds", "doc", registry=inner).observe(0.5)
    identity.set_agent("classifier")

    stamped = _samples(identity.IdentityRegistry(inner))
    assert stamped, "histogram produced no samples"
    for name, labels in stamped.items():
        assert labels.get("agent") == "classifier", f"{name} not stamped"
        assert "environment" in labels, f"{name} missing environment"


def test_label_set_is_stable_before_and_after_environment_is_known() -> None:
    inner = CollectorRegistry()
    prometheus_client.Counter("thing_total", "doc", registry=inner).inc()
    identity.set_agent("classifier")
    wrapper = identity.IdentityRegistry(inner)

    before = set(_samples(wrapper)["thing_total_total"])
    identity.set_environment("platform")
    after = set(_samples(wrapper)["thing_total_total"])

    assert before == after == {"agent", "environment"}


def test_restricted_registry_is_still_stamped() -> None:
    inner = CollectorRegistry()
    prometheus_client.Counter("kept_total", "doc", registry=inner).inc()
    prometheus_client.Counter("dropped_total", "doc", registry=inner).inc()
    identity.set_agent("classifier")

    restricted = identity.IdentityRegistry(inner).restricted_registry(["kept_total_total"])
    stamped = _samples(restricted)

    assert "dropped_total_total" not in stamped
    assert stamped["kept_total_total"]["agent"] == "classifier"
```

**Step 2: Run test to verify it fails**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_identity.py -v`
Expected: FAIL — `AttributeError: module 'ach_agent.engine.identity' has no attribute 'IdentityRegistry'`

**Step 3: Write minimal implementation**

Append to `src/ach_agent/engine/identity.py` (and add `from collections.abc import Iterable` plus
`from prometheus_client.registry import Collector, CollectorRegistry` to the imports):

```python
class IdentityRegistry:
    """Wraps a registry and stamps the process identity onto every sample.

    Operating at sample level rather than metric level means counters,
    histogram buckets, ``_sum``/``_count`` and ``_created`` series are all
    covered uniformly — and so is every metric added in the future, without
    anyone having to remember to label it. That was the scrape-side
    relabeling's one real advantage; this is how it is kept.

    Samples are rebuilt with ``_replace`` rather than a positional
    ``Sample(...)`` constructor: ``Sample`` gained ``native_histogram`` as a
    sixth field, and a positional rebuild silently drops any field added later.

    Existing metric labels win on collision. No ``ach_agent_*`` family declares
    ``agent`` or ``environment`` today, so this branch is defensive only.
    """

    def __init__(self, inner: CollectorRegistry) -> None:
        self._inner = inner

    def collect(self) -> Iterable[Collector]:
        stamp = current()
        for metric in self._inner.collect():
            metric.samples = [
                sample._replace(labels={**stamp, **sample.labels}) for sample in metric.samples
            ]
            yield metric

    def restricted_registry(self, names: Iterable[str]) -> IdentityRegistry:
        """Mirror ``CollectorRegistry.restricted_registry``.

        ``prometheus_client.exposition._bake_output`` calls this whenever the
        scrape URL carries ``name[]``. Without it that request raises instead of
        serving metrics.
        """
        return IdentityRegistry(self._inner.restricted_registry(names))
```

**Step 4: Run test to verify it passes**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_identity.py -v`
Expected: PASS (8 passed)

**Step 5: Commit**

```bash
git add src/ach_agent/engine/identity.py tests/engine/test_identity.py
git commit -m "feat(metrics): stamp agent identity on every exposed sample"
```

---

## Task 3: Mount the wrapper on /metrics

**Files:**
- Modify: `src/ach_agent/http/app.py:200-201`
- Test: `tests/http/test_metrics_identity.py`

**Step 1: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""The mounted /metrics endpoint must expose identity-stamped samples."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ach_agent.engine import identity


@pytest.fixture(autouse=True)
def _reset() -> None:
    identity.reset()


def test_metrics_endpoint_carries_identity(app_factory) -> None:
    identity.set_agent("classifier")
    identity.set_environment("platform")

    with TestClient(app_factory()) as client:
        body = client.get("/metrics/").text

    assert 'agent="classifier"' in body
    assert 'environment="platform"' in body
```

> `app_factory` is whatever fixture the existing `tests/http/` suite already uses to
> build the FastAPI app. Reuse it — do not construct a second app factory. Inspect
> `tests/http/` and `tests/conftest.py` first and adapt the call to match.

**Step 2: Run test to verify it fails**

Run: `./scripts/dev.sh uv run pytest tests/http/test_metrics_identity.py -v`
Expected: FAIL — assertion error, the body has no `agent=` label.

**Step 3: Write minimal implementation**

In `src/ach_agent/http/app.py`, add to the imports:

```python
from prometheus_client import REGISTRY

from ach_agent.engine.identity import IdentityRegistry
```

and replace line 200:

```python
    metrics_app = make_asgi_app(registry=IdentityRegistry(REGISTRY))
```

**Step 4: Run test to verify it passes**

Run: `./scripts/dev.sh uv run pytest tests/http/test_metrics_identity.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/ach_agent/http/app.py tests/http/test_metrics_identity.py
git commit -m "feat(metrics): serve /metrics through the identity registry"
```

---

## Task 4: Wire identity at boot and hydrate; delete `AGENT_INFO`

**Files:**
- Modify: `src/ach_agent/engine/hydrate.py:11,87-90`
- Modify: `src/ach_agent/engine/metrics.py:38-42` (delete)
- Modify: `src/ach_agent/main.py:1319` (call site — verify only, signature is unchanged)
- Test: `tests/engine/test_hydrate.py`

`AGENT_INFO` was added in `73f3ce5`, which is **after** the `v0.10.0` release commit and has
never been published — confirmed absent from the production Prometheus. There is no
compatibility surface to preserve; delete it outright rather than deprecating it.

**Step 1: Write the failing test**

Add to `tests/engine/test_hydrate.py` (match the existing mocking style in that file):

```python
async def test_hydrate_records_identity(...) -> None:
    """hydrate() sets both identity labels from config + manifest."""
    identity.reset()
    identity.set_agent("classifier")

    await hydrate(base_url, ek, "classifier")

    assert identity.current() == {"agent": "classifier", "environment": "platform"}
```

Fill the ellipses from the fixtures the surrounding tests already use to stub the
`/platform/hydrate` response; make the stubbed manifest return `environment="platform"`.

**Step 2: Run test to verify it fails**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_hydrate.py -v`
Expected: FAIL — `environment` is still `""` (hydrate sets the gauge, not the identity).

**Step 3: Write minimal implementation**

In `src/ach_agent/engine/hydrate.py`, replace the `AGENT_INFO` import on line 11 with:

```python
from ach_agent.engine import identity
```

and rewrite `hydrate()`:

```python
async def hydrate(base_url: str, ek: str, agent_name: str) -> HydrationManifest:
    manifest = await fetch_hydration_manifest(base_url, ek)
    identity.set_agent(agent_name)
    identity.set_environment(manifest.environment)
    return manifest
```

Delete the `AGENT_INFO` block from `src/ach_agent/engine/metrics.py` (lines 38-42) and drop the
now-unused `Gauge` reference. Leave the three counters untouched.

`main.py:1319` already passes `cfg.agent.name`; the signature does not change. Also call
`identity.set_agent(cfg.agent.name)` early in `main()` — before hydrate — so a scrape landing
during boot already carries the agent name.

**Step 4: Run the full suite**

Run: `make test`
Expected: PASS. Any test still importing `AGENT_INFO` fails here — delete those assertions,
they cover a metric that no longer exists.

**Step 5: Commit**

```bash
git add src/ach_agent/engine/hydrate.py src/ach_agent/engine/metrics.py src/ach_agent/main.py tests/
git commit -m "refactor(metrics): replace ach_agent_info gauge with stamped identity labels"
```

---

## Task 5: Outbound identity headers on model traffic

**Files:**
- Modify: `src/ach_agent/engine/mcp_proxy.py:83-102`
- Test: `tests/engine/test_mcp_proxy.py` (or the existing proxy test module — check first)

`_forward()` is the single choke point for both the MCP proxy and the model proxy; it builds
the outbound header dict at lines 101-102. Gate the injection on `label == "model"` so identity
is not attached to MCP traffic, whose upstreams have not been audited for this. Reading the
identity holder directly avoids threading a new parameter through `start()` and both handler
factories.

**Step 1: Write the failing test**

```python
async def test_model_forward_sends_identity_headers(...) -> None:
    identity.reset()
    identity.set_agent("classifier")
    identity.set_environment("platform")

    # forward a request through the model proxy against a stub upstream that
    # captures the headers it received
    assert captured["x-ach-agent"] == "classifier"
    assert captured["x-ach-environment"] == "platform"


async def test_mcp_forward_does_not_send_identity_headers(...) -> None:
    identity.reset()
    identity.set_agent("classifier")

    # same stub, label="mcp"
    assert "x-ach-agent" not in captured
```

Build both on whatever stub-upstream helper the existing proxy tests use.

**Step 2: Run test to verify it fails**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_mcp_proxy.py -v`
Expected: FAIL — `KeyError: 'x-ach-agent'`

**Step 3: Write minimal implementation**

In `src/ach_agent/engine/mcp_proxy.py`, after line 102 (`headers[auth_header] = auth_value`):

```python
    # Identity toward ACH on model traffic only (FinOps attribution). MCP upstreams are
    # excluded deliberately: they have not been audited as recipients of this metadata.
    if label == "model":
        stamp = identity.current()
        headers["x-ach-agent"] = stamp["agent"]
        headers["x-ach-environment"] = stamp["environment"]
```

Add `from ach_agent.engine import identity` to the imports.

**Step 4: Run test to verify it passes**

Run: `./scripts/dev.sh uv run pytest tests/engine/test_mcp_proxy.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/ach_agent/engine/mcp_proxy.py tests/engine/test_mcp_proxy.py
git commit -m "feat(proxy): send x-ach-agent and x-ach-environment on model traffic"
```

---

## Task 6: Docs

**Files:**
- Modify: `docs/configuration.md`
- Modify: `CHANGELOG.md`

**Step 1: Document the metric labels**

Add a short section stating that every `ach_agent_*` series carries `agent` and `environment`,
that both come from the harness (config + hydrate) and not from the scraper, and that
`environment` is empty for the few seconds between process start and a successful hydrate.

**Step 2: Document the outbound headers**

State that model-proxy traffic toward ACH carries `x-ach-agent` and `x-ach-environment`, and
that MCP traffic does not.

**Step 3: Changelog**

Record the label addition as a breaking observability change: series identity changes, so range
queries spanning the upgrade show a break. Record the removal of `ach_agent_info` (never
released).

**Step 4: Commit**

```bash
git add docs/configuration.md CHANGELOG.md
git commit -m "docs(metrics): document agent identity labels and outbound headers"
```

---

## Task 7: Lint, full suite, release

**Step 1: Lint**

Run: `make lint`
Expected: PASS (ruff check + format --check + mypy --strict)

**Step 2: Full suite**

Run: `make test`
Expected: PASS

**Step 3: Tag the missing v0.10.0**

`aec85d1 chore(release): v0.10.0` was merged but never tagged, so there is no immutable
version to pin — production tracks the mutable `:latest`. Tag it before cutting the next one:

```bash
git tag -a v0.10.0 aec85d1 -m "v0.10.0"
git push origin v0.10.0
```

**Step 4: Release this work**

Bump to `v0.11.0` following the repo's existing release commit convention, tag, and push.

**Step 5: Verify in production**

After the agent Deployments roll (`imagePullPolicy: Always` on `:latest`):

```bash
kubectl -n ach rollout restart deploy -l app.kubernetes.io/name=ach-agent
```

Then query Prometheus and confirm the label is present and correct:

```
sum by (agent, environment) (ach_agent_turns_total)
```

Expected: one series per active agent, `environment="platform"` (or `zohodesk` for
`zohodesk-joan`). Before this change the query returns a single unlabelled series.

---

## Companion change in repo `ach`

Not part of this plan's execution — track it separately, it ships in ACH `v0.6.23`.

**File:** `deploy/helm/ach/templates/podmonitor.yaml`

Delete the `relabelings:` block (the `__meta_kubernetes_pod_label_ach_ackstorm_ai_agent` →
`agent` mapping). **Keep** `path: /metrics/` — the trailing slash avoids a 307 redirect and is
unrelated to identity. The PodMonitor reverts to pure scrape configuration.

Sequencing was decided explicitly: no bridge. If ACH `v0.6.23` ships before ach-agent
`v0.11.0`, there is a window with no `agent` label anywhere. The agent dashboards have been
unpopulated since 2026-07-24 regardless, so this is not a regression — only a delayed
improvement.

## Out of scope

- `spec.cost` on the ACH CRD (`AgentProfile.spec.achagent.cost` / `ACHAgent.spec.cost`) — owned separately.
- Turning the new headers into LiteLLM tags so `litellm_spend_metric_total` gains a per-agent dimension — a later phase. Note that per-**environment** attribution already exists for free via `team="ach-env-<env>"`, because `ek_` keys are minted into that shell team.

