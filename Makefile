VERSION ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' runtime/pyproject.toml)
PY ?= runtime/.venv/bin/python

.PHONY: chart-sync chart-test release-check release-bump e2e-up e2e-down e2e-run e2e

chart-sync: ## copy CRDs and render the ClusterRole from operator/config
	$(PY) hack/chart-sync.py

chart-test: chart-sync ## lint and render-test the chart; fails on drift in chart/
	@/usr/bin/git diff --exit-code -- chart || { echo "FAIL: chart/ drifted; commit the output of make chart-sync" >&2; exit 1; }
	helm lint chart --set openshell.gateway=http://x:8080
	PATH="$(CURDIR)/runtime/.venv/bin:$$PATH" bash chart/tests/render_test.sh
