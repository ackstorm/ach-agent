#!/usr/bin/env bash
# Brings up / tears down the kind cluster ach-agent-e2e: Istio ambient, agent-sandbox, a real
# OpenShell gateway, the stub upstreams and the ach-agent chart. Never touches another cluster.
# Usage: cluster.sh up|load|down
set -euo pipefail

CLUSTER=ach-agent-e2e
CTX=kind-$CLUSTER
ISTIO_VERSION=1.28.3
OPENSHELL_VERSION=0.1.5
SANDBOX_VERSION=v1.0.8
AGENT_NS=ach-agent-e2e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/test/e2e"

k() { kubectl --context "$CTX" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }
log() { echo "[cluster.sh] $*"; }

guard() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kubectl config get-contexts -o name | grep -qx "$CTX" ||
      { echo "FAIL: kind cluster $CLUSTER exists but context $CTX is missing" >&2; exit 1; }
  fi
}

create_cluster() {
  if kind get clusters | grep -qx "$CLUSTER"; then
    log "kind cluster $CLUSTER exists"
  else
    # kind makes the new context current; put the caller's back so other work is unaffected.
    local prev
    prev="$(kubectl config current-context 2>/dev/null || true)"
    kind create cluster --config "$HERE/kind-config.yaml" --wait 120s
    [ -z "$prev" ] || kubectl config use-context "$prev" >/dev/null
  fi
  guard
}

install_prereqs() {
  log "agent-sandbox $SANDBOX_VERSION"
  k apply --server-side -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$SANDBOX_VERSION/sandbox-with-extensions.yaml"
  k -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s

  log "istio ambient $ISTIO_VERSION"
  helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update >/dev/null
  h upgrade --install istio-base istio/base --version "$ISTIO_VERSION" -n istio-system --create-namespace --wait --timeout 6m
  h upgrade --install istiod istio/istiod --version "$ISTIO_VERSION" -n istio-system --set profile=ambient --wait --timeout 6m
  # dnsCapture=false: ztunnel's DNS proxy answers the sandbox supervisor's TCP DNS probe malformed.
  h upgrade --install istio-cni istio/cni --version "$ISTIO_VERSION" -n istio-system \
    --set profile=ambient --set ambient.dnsCapture=false --wait --timeout 6m
  h upgrade --install ztunnel istio/ztunnel --version "$ISTIO_VERSION" -n istio-system --wait --timeout 6m

  for ns in openshell "$AGENT_NS" ach-agent-system; do
    k create namespace "$ns" --dry-run=client -o yaml | k apply -f -
  done
  k label namespace openshell "$AGENT_NS" istio.io/dataplane-mode=ambient --overwrite

  log "openshell $OPENSHELL_VERSION"
  h upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version "$OPENSHELL_VERSION" \
    -n openshell -f "$HERE/openshell-values.yaml" --wait --timeout 6m
  sed "s/<AGENT_NAMESPACE>/$AGENT_NS/" "$ROOT/docs/mesh/openshell-mesh.yaml" | k apply -f -
}

build_images() {
  log "building images"
  docker build -q -t ach-agent-operator:e2e "$ROOT/operator"
  docker build -q --target control -t ach-agent-control:e2e "$ROOT/runtime"
  docker build -q --target execution -t ach-agent-execution:e2e "$ROOT/runtime"
  docker build -q -t ach-agent-e2e-stub:e2e "$HERE/stubs"
  for img in ach-agent-operator ach-agent-control ach-agent-execution ach-agent-e2e-stub; do
    kind load docker-image "$img:e2e" --name "$CLUSTER"
  done
}

deploy() {
  k apply -f "$HERE/stub.yaml"
  h upgrade --install ach-agent "$ROOT/chart" -n ach-agent-system \
    --set image.operator=ach-agent-operator --set image.tag=e2e \
    --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080 \
    --wait --timeout 6m
  k -n "$AGENT_NS" rollout status deploy/e2e-stub --timeout=120s
}

case "${1:-}" in
  up)
    create_cluster
    install_prereqs
    build_images
    deploy
    ;;
  load)
    guard
    build_images
    k -n ach-agent-system rollout restart deploy/ach-agent-operator
    k -n "$AGENT_NS" rollout restart deploy/e2e-stub
    k -n ach-agent-system rollout status deploy/ach-agent-operator --timeout=300s
    k -n "$AGENT_NS" rollout status deploy/e2e-stub --timeout=120s
    ;;
  down)
    kind delete cluster --name "$CLUSTER"
    ;;
  *)
    echo "usage: $0 up|load|down" >&2
    exit 2
    ;;
esac
