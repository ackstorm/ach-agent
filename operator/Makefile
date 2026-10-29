export GOTOOLCHAIN := go1.26.10
CONTROLLER_GEN := go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0
ENVTEST := go run sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.24
ENVTEST_K8S_VERSION := 1.36.x
ENVTEST_BIN := $(CURDIR)/bin/envtest
UNIT_PKGS = $$(go list ./... | grep -v /internal/controller)

.PHONY: gen test test-envtest verify build

gen: ## deepcopy, CRDs and RBAC
	$(CONTROLLER_GEN) object:headerFile=hack/boilerplate.go.txt paths="./api/..."
	$(CONTROLLER_GEN) rbac:roleName=ach-agent-operator crd paths="./api/..." paths="./internal/..." \
	  output:crd:artifacts:config=config/crd output:rbac:artifacts:config=config/rbac

build:
	go build ./...

test: ## unit tests (no cluster)
	go vet ./...
	go test -race -count=1 $(UNIT_PKGS)

test-envtest: gen ## controller envtest
	KUBEBUILDER_ASSETS="$$($(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(ENVTEST_BIN) -p path)" \
	  go test -race -count=1 -timeout 15m ./internal/controller/...

verify: gen test
	@/usr/bin/git diff --exit-code -- config api || { echo "FAIL: generated files drifted; run make gen" >&2; exit 1; }
