# ach-agent — all targets run inside the devtools container (no host pip/venv).
# Public target `foo` re-execs `_foo` via scripts/dev.sh unless IN_DEVTOOLS=1.
SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

OWNER ?= ackstorm
IMG   ?= ghcr.io/$(OWNER)/ach-agent
APP_DIR := src/ach_agent

IN_DEVTOOLS ?=
define container_target
	@if [ "$(IN_DEVTOOLS)" = "1" ]; then \
		$(MAKE) --no-print-directory $(1); \
	else \
		./scripts/dev.sh $(MAKE) --no-print-directory $(1); \
	fi
endef

##@ General
.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Setup
.PHONY: hooks
hooks: ## Install git pre-push hook
	./scripts/install-hooks.sh

.PHONY: devtools-image
devtools-image: ## Build the devtools image locally
	docker build -t ghcr.io/$(OWNER)/ach-agent-devtools:$$(sha256sum docker/devtools/Dockerfile | cut -c1-12) -f docker/devtools/Dockerfile .

##@ Dev (containerized)
.PHONY: deps _deps
deps: ## Install project deps (uv sync --frozen)
	$(call container_target,_deps)
_deps:
	uv sync --frozen

.PHONY: lint _lint
lint: ## ruff check + format --check + mypy --strict
	$(call container_target,_lint)
_lint:
	uv run ruff check $(APP_DIR) && uv run ruff format --check $(APP_DIR) && uv run mypy --strict $(APP_DIR)

.PHONY: fmt _fmt
fmt: ## ruff format (mutates)
	$(call container_target,_fmt)
_fmt:
	uv run ruff check --fix $(APP_DIR) && uv run ruff format $(APP_DIR)

.PHONY: test _test
test: ## pytest (all tests, excluding e2e)
	$(call container_target,_test)
_test:
	uv run pytest tests/ -q --ignore=tests/e2e

.PHONY: test-fast _test-fast
test-fast: test ## alias of test (kept for hooks / muscle memory)
_test-fast: _test

.PHONY: conformance _conformance
conformance: ## Run CONTRACT §6 conformance suite (11 named invariants, D-10)
	$(call container_target,_conformance)
_conformance:
	uv run pytest tests/conformance/ -v

##@ Maintenance (containerized)
.PHONY: clean _clean
clean: ## Prune stale uv cache + remove local caches (conservative)
	$(call container_target,_clean)
_clean:
	uv cache prune
	find $(APP_DIR) tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .mypy_cache .pytest_cache

.PHONY: clean-all _clean-all
clean-all: ## Wipe the entire uv cache + remove local caches
	$(call container_target,_clean-all)
_clean-all:
	uv cache clean
	find $(APP_DIR) tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .mypy_cache .pytest_cache

##@ Security (host docker — secret scanning)
.PHONY: secrets
secrets: ## gitleaks + trufflehog over the working tree
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:latest \
		detect --source=/repo --redact --no-banner --config=/repo/.gitleaks.toml
	docker run --rm -v "$(CURDIR):/pwd:ro" trufflesecurity/trufflehog:latest \
		git file:///pwd --only-verified --fail --no-update

##@ Docs (containerized)
.PHONY: docs-build _docs-build
docs-build: ## mkdocs build --strict
	$(call container_target,_docs-build)
_docs-build:
	uv run mkdocs build --strict

.PHONY: docs-serve
docs-serve: ## Live docs preview on :8000
	./scripts/dev.sh uv run mkdocs serve -a 0.0.0.0:8000

##@ Contract
.PHONY: schema _schema
schema: ## Regenerate the frozen JSON Schema v1 from AgentConfig (docs/schemas/agent-config-v1.schema.json)
	$(call container_target,_schema)
_schema:
	uv run python scripts/gen_schema.py

##@ Verify
.PHONY: verify
verify: lint test conformance secrets ## Full local gate (lint + mypy + unit + conformance + secrets)

##@ Build / Release
.PHONY: build-image
build-image: ## Build the runtime container image
	docker build -t $(IMG):dev -f Dockerfile .

.PHONY: release-bump
release-bump: ## Bump version everywhere (VERSION=X.Y.Z)
	@test -n "$(VERSION)" || { echo "VERSION=X.Y.Z required"; exit 1; }
	@echo "$(VERSION)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([-.A-Za-z0-9]+)?$$' || { echo "bad VERSION '$(VERSION)'"; exit 1; }
	sed -i -E 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml
	sed -i -E 's/^## \[unreleased\].*/## [unreleased]\n\n## [$(VERSION)] - '"$$(date +%F)"'/' CHANGELOG.md
	@echo "Bumped to $(VERSION). Review the diff + CHANGELOG.md, commit, then: make release-cut VERSION=$(VERSION)"

.PHONY: release-cut
release-cut: ## Tag-trigger a release (VERSION=X.Y.Z) — empty commit on main (pushes)
	@test -n "$(VERSION)" || { echo "VERSION=X.Y.Z required"; exit 1; }
	@git diff --quiet || { echo "working tree dirty — commit the release-bump first"; exit 1; }
	./scripts/pre-push-check.sh
	git commit --allow-empty -m "chore(release): v$(VERSION)"
	git push origin HEAD
