# ach-agent

One project owns agents end to end: CRDs, operator and runtime. It runs on its own by
default, with models and MCP servers configured directly (for example OpenRouter, LiteLLM,
Serper). ACH is an optional capability server. OpenShell is the only execution sandbox, and
a service mesh (Istio ambient) is required.

The repository holds two languages under one version: `operator/` (Go) and `runtime/`
(Python, package `ach_runtime`), plus the Helm chart, end-to-end tests and docs.

Status: under construction; see docs/superpowers/specs.
