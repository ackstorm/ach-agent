.PHONY: help hooks verify release-bump release-cut spike-opencode spike-opencode-remainder e2e compose-e2e
.DEFAULT_GOAL := help

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  %-20s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

hooks: ## Install the git pre-push hook
	./scripts/install-hooks.sh

verify: ## Pre-push gate (gitleaks, SPDX, schema drift, config tests)
	./scripts/pre-push-check.sh

release-bump: ## Bump version in pyproject.toml + CHANGELOG (VERSION=X.Y.Z)
	@test -n "$(VERSION)" || { echo "VERSION=X.Y.Z required"; exit 1; }
	@echo "$(VERSION)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$$' || { echo "bad VERSION '$(VERSION)'"; exit 1; }
	sed -i -E '0,/^version = ".*"/s//version = "$(VERSION)"/' pyproject.toml
	sed -i -E 's/^## \[unreleased\].*/## [unreleased]\n\n## [$(VERSION)] - '"$$(date +%F)"'/' CHANGELOG.md
	@echo "Bumped to $(VERSION). Review the diff, commit, merge to main, then: make release-cut VERSION=$(VERSION)"

release-cut: ## Trigger the release workflow: empty chore(release) commit on main (pushes)
	@test -n "$(VERSION)" || { echo "VERSION=X.Y.Z required"; exit 1; }
	@test "$$(git branch --show-current)" = main || { echo "must be on main"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "working tree dirty"; exit 1; }
	@grep -q '^version = "$(VERSION)"' pyproject.toml || { echo "pyproject.toml version != $(VERSION): run release-bump"; exit 1; }
	./scripts/pre-push-check.sh
	git commit --allow-empty -m "chore(release): v$(VERSION)"
	git push origin HEAD

CASE ?= all

# Builds the devtools image (pins the real OpenCode v2 release) and runs the
# Task 1 compatibility probe inside it. See spikes/opencode-v2/README.md.
spike-opencode:
	docker build -f Dockerfile.devtools -t ach-spike-opencode-v2 .
	docker run --rm ach-spike-opencode-v2

# Remainder probes: catalog/cancel/shell/output/compaction/stepreset/resume/
# snapshot/controls/ownership. CASE selects one scenario (e.g.
# `make spike-opencode-remainder CASE=catalog`); CASE=all runs every case.
# Current coordinator verification: 121/121 PASS, 0 FAIL, 0 UNVERIFIED;
# LSP behavioural support is explicitly UNSCORED. See the compatibility report.
spike-opencode-remainder:
	docker build -f Dockerfile.devtools -t ach-spike-opencode-v2 .
	docker run --rm -e PROBE_CASE=$(CASE) ach-spike-opencode-v2 node spikes/opencode-v2/remainder.mjs

# --- Task 2A: src/ach_runtime config schema/canonicalization (container, no host venv) ---
.PHONY: test-config gen-schema schema-check

test-config:
	docker build --target python-config -f Dockerfile.devtools -t ach-runtime-config-devtools .
	docker run --rm ach-runtime-config-devtools pytest tests/config -q

gen-schema:
	docker build --target python-config -f Dockerfile.devtools -t ach-runtime-config-devtools .
	docker run --rm -v $(CURDIR)/docs/schemas:/work/docs/schemas ach-runtime-config-devtools python scripts/gen_schema.py

schema-check:
	docker build --target python-config -f Dockerfile.devtools -t ach-runtime-config-devtools .
	docker run --rm ach-runtime-config-devtools python scripts/gen_schema.py --check

# --- Task 4: execution/ + engine/ + plugins/managed-config/ (container, pinned OpenCode v2) ---
.PHONY: test-execution

test-execution:
	docker build --target python-execution -f Dockerfile.devtools -t ach-runtime-execution-devtools .
	docker run --rm ach-runtime-execution-devtools

# --- Task 6A: storage/ durable CAS core (container, no host venv) ---
.PHONY: test-storage test-unit

test-storage:
	docker build --target python-config -f Dockerfile.devtools -t ach-runtime-config-devtools .
	docker run --rm ach-runtime-config-devtools pytest tests/storage -q

# Native/model/tool/S3 proof against the already-built integration image and the
# already-running SeaweedFS compose network. This target never builds or pulls images.
E2E_IMAGE ?= ach-runtime-coordinator-integration:latest
E2E_NETWORK ?= ach-runtime-seaweed-e2e_test
E2E_S3_ENDPOINT ?= http://s3:8333

e2e:
	docker image inspect "$(E2E_IMAGE)" >/dev/null
	docker network inspect "$(E2E_NETWORK)" >/dev/null
	timeout 300s docker run --rm --pull=never --cpus=1 --memory=1g --memory-swap=1g --pids-limit=256 --network="$(E2E_NETWORK)" \
		--tmpfs /tmp:rw,nosuid,nodev,size=512m \
		-v "$(CURDIR)/src:/work/src:ro" -v "$(CURDIR)/tests:/work/tests:ro" \
		-v "$(CURDIR)/plugins:/work/plugins:ro" \
		-e PYTHONPATH=/work/src:/work/tests/storage:/work/tests/engine:/work/tests/facades:/work/tests/harness \
		-e PYTHONDONTWRITEBYTECODE=1 -e OPENCODE_BIN=/work/node_modules/.bin/opencode2 \
		-e ACH_STORAGE_TEST_S3_ENDPOINT="$(E2E_S3_ENDPOINT)" -e ACH_STORAGE_TEST_S3_BUCKET=ach-storage-test \
		-e ACH_STORAGE_TEST_S3_ACCESS_KEY=ach-storage-test -e ACH_STORAGE_TEST_S3_SECRET_KEY=ach-storage-test-secret \
		-e AWS_ACCESS_KEY_ID=ach-storage-test -e AWS_SECRET_ACCESS_KEY=ach-storage-test-secret -e AWS_DEFAULT_REGION=us-east-1 \
		"$(E2E_IMAGE)" sh -ec ' \
			"$$OPENCODE_BIN" --version | grep -Fx "opencode v2.0.20"; \
			node --version >/dev/null; \
			python -c "import boto3, pytest, httpx, uvicorn, ach_runtime"; \
			pytest -q -p no:cacheprovider --junitxml=/tmp/ach-e2e-required.xml tests/e2e/test_pr_review.py tests/e2e/test_recovery.py tests/e2e/test_host_scenarios.py tests/storage/test_s3_integration.py; \
			python -c "import sys, xml.etree.ElementTree as ET; root=ET.parse(sys.argv[1]).getroot(); cases=list(root.iter(\"testcase\")); skipped=sum(1 for case in cases for node in case if node.tag == \"skipped\"); failures=sum(1 for case in cases for node in case if node.tag == \"failure\"); errors=sum(1 for case in cases for node in case if node.tag == \"error\"); assert cases and skipped == failures == errors == 0; print(\"mandatory E2E suite: tests=%d skipped=%d failures=%d errors=%d\" % (len(cases), skipped, failures, errors))" /tmp/ach-e2e-required.xml; \
			pytest -q -p no:cacheprovider --junitxml=/tmp/ach-e2e-native-compat.xml tests/engine/test_opencode_adapter.py::test_double_start_same_invocation_id_reuses_one_native_session; \
			python -c "import sys, xml.etree.ElementTree as ET; root=ET.parse(sys.argv[1]).getroot(); cases=list(root.iter(\"testcase\")); skipped=sum(1 for case in cases for node in case if node.tag == \"skipped\"); failures=sum(1 for case in cases for node in case if node.tag == \"failure\"); errors=sum(1 for case in cases for node in case if node.tag == \"error\"); assert cases and skipped == failures == errors == 0; print(\"mandatory native compatibility case: tests=%d skipped=%d failures=%d errors=%d\" % (len(cases), skipped, failures, errors))" /tmp/ach-e2e-native-compat.xml'

# Single-unit acceptance test: builds the agent image and runs the compose example (spec 12).
# Separate from `e2e`, which has a hard 300 s cap.
compose-e2e:
	timeout 1500 bash examples/hosts/compose/ci/run.sh

# The SeaweedFS fixture CI starts; its credentials are the fixture's own, not secrets.
ACH_STORAGE_TEST_S3_ENDPOINT ?= http://127.0.0.1:19000
ACH_STORAGE_TEST_S3_BUCKET ?= ach-storage-test
ACH_STORAGE_TEST_S3_ACCESS_KEY ?= ach-storage-test
ACH_STORAGE_TEST_S3_SECRET_KEY ?= ach-storage-test-secret
export ACH_STORAGE_TEST_S3_ENDPOINT ACH_STORAGE_TEST_S3_BUCKET ACH_STORAGE_TEST_S3_ACCESS_KEY ACH_STORAGE_TEST_S3_SECRET_KEY

.PHONY: s3-fixture
s3-fixture: ## Start the local S3 fixture the storage and e2e tests need
	docker network inspect ach-runtime-seaweed-e2e_test >/dev/null 2>&1 || docker network create ach-runtime-seaweed-e2e_test
	timeout 180 docker compose --profile fixture up -d --wait --wait-timeout 120 s3

test-unit: s3-fixture ## Full unit suite in the devtools execution image, as CI runs it
	docker build --target python-execution -f Dockerfile.devtools -t ach-agent-runtime-execution-devtools .
	docker run --rm --network host -e PYTHONDONTWRITEBYTECODE=1 \
	  -e PYTHONPATH=/work/src:/work/tests/storage:/work/tests/engine:/work/tests/facades:/work/tests/harness \
	  -e ACH_STORAGE_TEST_S3_ENDPOINT -e ACH_STORAGE_TEST_S3_BUCKET \
	  -e ACH_STORAGE_TEST_S3_ACCESS_KEY -e ACH_STORAGE_TEST_S3_SECRET_KEY \
	  -v "$(CURDIR)/src:/work/src:ro" -v "$(CURDIR)/tests:/work/tests:ro" \
	  -v "$(CURDIR)/plugins:/work/plugins:ro" -v "$(CURDIR)/docs:/work/docs:ro" \
	  -v "$(CURDIR)/examples:/work/examples:ro" -v "$(CURDIR)/scripts:/work/scripts:ro" \
	  -v "$(CURDIR)/Dockerfile:/work/Dockerfile:ro" -w /work \
	  ach-agent-runtime-execution-devtools \
	  pytest --import-mode=importlib tests -q -p no:cacheprovider
