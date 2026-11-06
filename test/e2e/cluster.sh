#!/usr/bin/env bash
# Brings up / tears down the kind cluster ach-agent-e2e: Istio ambient, agent-sandbox, a real
# OpenShell gateway, the stub upstreams and the ach-agent chart. Never touches another cluster.
# Usage: cluster.sh up|load|down
set -euo pipefail

CLUSTER=ach-agent-e2e
CTX=kind-$CLUSTER
ISTIO_VERSION=1.28.3
OPENSHELL_VERSION=0.1.7
SANDBOX_VERSION=v1.0.8
CERT_MANAGER_VERSION=v1.19.1
AGENT_NS=ach-agent-e2e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/test/e2e"
STUB_HOST=e2e-stub.$AGENT_NS.svc.cluster.local
TLS_DIR="$HERE/.tls"

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

# The gateway PKI (docs/mesh/openshell-pki.yaml, all in openshell). cert-manager's webhook refuses
# requests until its serving certificate is injected, so the first applies may fail.
install_pki() {
  local applied="" s keys key
  for _ in $(seq 1 30); do
    if sed "s/<AGENT_NAMESPACE>/$AGENT_NS/" "$ROOT/docs/mesh/openshell-pki.yaml" | k apply -f - >/dev/null 2>&1; then
      applied=1
      break
    fi
    sleep 5
  done
  [ -n "$applied" ] || { echo "FAIL: cert-manager did not accept docs/mesh/openshell-pki.yaml within 150s" >&2; exit 1; }
  k -n openshell wait --for=condition=Ready certificate/openshell-server certificate/openshell-client \
    certificate/ach-agent-operator --timeout=180s
  # The OpenShell chart and the operator read tls.crt, tls.key and ca.crt (key names only are listed).
  for s in openshell-server-tls openshell-client-tls ach-agent-operator-tls; do
    keys="$(k -n openshell get secret "$s" -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}')"
    for key in ca.crt tls.crt tls.key; do
      case " $keys" in *" $key "*) ;; *) echo "FAIL: Secret $s has no $key" >&2; exit 1 ;; esac
    done
  done
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

  log "cert-manager $CERT_MANAGER_VERSION"
  helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
  h upgrade --install cert-manager jetstack/cert-manager --version "$CERT_MANAGER_VERSION" -n cert-manager \
    --create-namespace --set crds.enabled=true --wait --timeout 6m
  install_pki

  log "openshell $OPENSHELL_VERSION"
  local server_crt
  server_crt="$(k -n openshell get secret openshell-server-tls -o jsonpath='{.data.tls\.crt}')"
  h upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version "$OPENSHELL_VERSION" \
    -n openshell -f "$HERE/openshell-values.yaml" -f "$HERE/openshell-tls-values.yaml" --wait --timeout 6m
  [ "$(k -n openshell get secret openshell-server-tls -o jsonpath='{.data.tls\.crt}')" = "$server_crt" ] ||
    { echo "FAIL: the OpenShell certgen hook replaced the cert-manager certificate in openshell-server-tls" >&2; exit 1; }
  sed "s/<AGENT_NAMESPACE>/$AGENT_NS/" "$ROOT/docs/mesh/openshell-mesh.yaml" | k apply -f -
}

gen_tls() {
  if [ -s "$TLS_DIR/tls.crt" ] && openssl x509 -checkend 604800 -noout -in "$TLS_DIR/tls.crt" >/dev/null; then
    return 0
  fi
  log "generating the exchange stub's CA and certificate"
  mkdir -p "$TLS_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=ach-agent-e2e-ca" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$STUB_HOST" \
    -keyout "$TLS_DIR/tls.key" -out "$TLS_DIR/tls.csr" 2>/dev/null
  printf '%s\n' "subjectAltName=DNS:$STUB_HOST" "basicConstraints=CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" "extendedKeyUsage=serverAuth" \
    "subjectKeyIdentifier=hash" "authorityKeyIdentifier=keyid,issuer" > "$TLS_DIR/ext.cnf"
  openssl x509 -req -in "$TLS_DIR/tls.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
    -days 30 -extfile "$TLS_DIR/ext.cnf" -out "$TLS_DIR/tls.crt" 2>/dev/null
}

build_images() {
  log "building images"
  docker build -q -t ach-agent-operator:e2e "$ROOT/operator"
  docker build -q --target control -t ach-agent-control:e2e "$ROOT/runtime"
  docker build -q -t ach-agent-control:e2e -f "$HERE/control-ca.Dockerfile" "$TLS_DIR"
  docker build -q --target execution -t ach-agent-execution:e2e "$ROOT/runtime"
  docker build -q --target execution-pi -t ach-agent-execution-pi:e2e "$ROOT/runtime"
  docker build -q -t ach-agent-e2e-stub:e2e "$HERE/stubs"
  for img in ach-agent-operator ach-agent-control ach-agent-execution ach-agent-execution-pi ach-agent-e2e-stub; do
    kind load docker-image "$img:e2e" --name "$CLUSTER"
  done
}

deploy() {
  k -n "$AGENT_NS" create secret tls e2e-stub-tls --cert "$TLS_DIR/tls.crt" --key "$TLS_DIR/tls.key" \
    --dry-run=client -o yaml | k apply -f -
  k apply -f "$HERE/stub.yaml"
  k apply -f "$HERE/s3-standin.yaml"
  k -n ach-agent-e2e-s3 rollout status deploy/s3-standin --timeout=180s
  k -n ach-agent-e2e-s3 exec deploy/s3-standin -- sh -c \
    'printf "s3.bucket.list\n" | weed shell -master=127.0.0.1:9333 | grep -qw ach-e2e ||
     printf "s3.bucket.create -name ach-e2e\n" | weed shell -master=127.0.0.1:9333'
  k -n "$AGENT_NS" create secret generic e2e-s3 --from-literal=AWS_ACCESS_KEY_ID=e2e-s3 \
    --from-literal=AWS_SECRET_ACCESS_KEY=e2e-s3-secret --dry-run=client -o yaml | k apply -f -
  # helm never updates CRDs of an installed release.
  k apply --server-side --force-conflicts -f "$ROOT/chart/crds"
  h upgrade --install ach-agent "$ROOT/chart" -n ach-agent-system \
    --set image.operator=ach-agent-operator --set image.tag=e2e \
    --set openshell.gateway=https://openshell.openshell.svc.cluster.local:8080 \
    --set openshell.tls.secret.namespace=openshell --set openshell.tls.secret.name=ach-agent-operator-tls \
    --set openshell.workspace=ach-agent \
    --set runtime.storage.s3.bucket=ach-e2e --set runtime.storage.s3.region=us-east-1 \
    --set runtime.storage.s3.endpointUrl=http://s3-standin.ach-agent-e2e-s3.svc.cluster.local:8333 \
    --set runtime.storage.s3.credentialsSecret=e2e-s3 \
    --set "storage.namespaces={$AGENT_NS}" --set storage.size=2Gi \
    --wait --timeout 6m
  # The :e2e tag does not change, so helm leaves a running operator on its old image.
  k -n ach-agent-system rollout restart deploy/ach-agent-operator
  k -n ach-agent-system rollout status deploy/ach-agent-operator --timeout=300s
  k -n "$AGENT_NS" rollout status deploy/e2e-stub --timeout=120s
}

case "${1:-}" in
  up)
    create_cluster
    install_prereqs
    gen_tls
    build_images
    deploy
    ;;
  load)
    guard
    gen_tls
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
