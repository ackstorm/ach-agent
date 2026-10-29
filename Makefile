VERSION ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' runtime/pyproject.toml)
PY ?= runtime/.venv/bin/python

.PHONY: chart-sync chart-test release-check release-bump e2e-up e2e-down e2e-run e2e

chart-sync: ## copy CRDs and render the ClusterRole from operator/config
	$(PY) hack/chart-sync.py

chart-test: chart-sync ## lint and render-test the chart; fails on drift in chart/
	@/usr/bin/git diff --exit-code -- chart || { echo "FAIL: chart/ drifted; commit the output of make chart-sync" >&2; exit 1; }
	helm lint chart --set openshell.gateway=http://x:8080
	PATH="$(CURDIR)/runtime/.venv/bin:$$PATH" bash chart/tests/render_test.sh

e2e-up: ## create the ach-agent-e2e kind cluster and deploy everything
	test/e2e/cluster.sh up

e2e-down: ## delete the ach-agent-e2e kind cluster
	test/e2e/cluster.sh down

e2e-run: ## run the e2e scenarios against a running cluster
	cd test/e2e && go test -tags e2e -count=1 -timeout 55m -v ./...

e2e: e2e-up e2e-run

release-check: ## runtime, chart and CHANGELOG agree on VERSION (default: the runtime version)
	@v="$(VERSION)"; \
	chart_v=$$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\}$$/\1/p' chart/Chart.yaml); \
	chart_a=$$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}$$/\1/p' chart/Chart.yaml); \
	run_v=$$(sed -n 's/^version = "\(.*\)"/\1/p' runtime/pyproject.toml); \
	[ -n "$$v" ] && [ "$$run_v" = "$$v" ] && [ "$$chart_v" = "$$v" ] && [ "$$chart_a" = "$$v" ] || \
	  { echo "FAIL: versions differ: want $$v, runtime=$$run_v chart=$$chart_v appVersion=$$chart_a" >&2; exit 1; }; \
	grep -Fq "## [$$v]" CHANGELOG.md || { echo "FAIL: CHANGELOG.md has no ## [$$v]" >&2; exit 1; }; \
	echo "release-check ok: $$v"

release-bump: ## set runtime and chart versions and add a CHANGELOG heading (VERSION=X.Y.Z); never commits
	@test -n "$(VERSION)" || { echo "usage: make release-bump VERSION=X.Y.Z" >&2; exit 2; }
	sed -i -E 's/^version = ".*"/version = "$(VERSION)"/' runtime/pyproject.toml
	sed -i -E 's/^version: .*/version: $(VERSION)/; s/^appVersion: .*/appVersion: "$(VERSION)"/' chart/Chart.yaml
	grep -Fq "## [$(VERSION)]" CHANGELOG.md || \
	  sed -i -E '0,/^## \[/s//## [$(VERSION)] - '"$$(date +%F)"'\n\n&/' CHANGELOG.md
	@echo "Bumped to $(VERSION). Edit CHANGELOG.md, then commit chore(release): v$(VERSION) on main."
