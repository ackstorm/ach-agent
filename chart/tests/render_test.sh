#!/usr/bin/env bash
# Renders the chart and asserts the security-relevant output. Needs helm and python3 with PyYAML.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
base=(--namespace ach-agent-system --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080)

helm template t . --namespace ach-agent-system >/dev/null 2>&1 && fail "renders without openshell.gateway"
helm template t . "${base[@]}" --set openshell.mesh=none >/dev/null 2>&1 && fail "renders with mesh=none"

out=$(helm template t . "${base[@]}")
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
def one(kind):
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, (kind, len(found))
    return found[0]
dep = one("Deployment")
pod = dep["spec"]["template"]
assert pod["metadata"]["labels"]["istio.io/dataplane-mode"] == "ambient"
c = pod["spec"]["containers"][0]
env = {e["name"]: e for e in c.get("env", [])}
assert env["ACH_AGENT_OPENSHELL_GATEWAY"]["value"] == "http://openshell.openshell.svc.cluster.local:8080"
assert env["ACH_AGENT_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"
assert c["image"].endswith(":0.3.0-rc.2"), c["image"]
sc = c["securityContext"]
assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
role = one("Role")
assert any("leases" in r["resources"] for r in role["rules"]), role
vap = one("ValidatingAdmissionPolicy")
exprs = " ".join(v["expression"] for v in vap["spec"]["validations"])
assert "ach-agent-key-" in exprs and "Opaque" in exprs, exprs
one("ValidatingAdmissionPolicyBinding")
cr = one("ClusterRole")
for r in cr["rules"]:
    assert "*" not in r.get("verbs", []) and "*" not in r.get("resources", []), r
    assert "leases" not in r.get("resources", []), r
assert not [d for d in docs if d["kind"] in ("StatefulSet", "AuthorizationPolicy")], "storage renders without a bucket"
print("render ok")
PY
for f in dashboards/*.json; do python3 -m json.tool "$f" >/dev/null || fail "$f is not valid JSON"; done

s3=(--set runtime.storage.s3.bucket=ach-bucket --set runtime.storage.s3.region=eu-west-1)
helm template t . "${base[@]}" "${s3[@]}" >/dev/null 2>&1 && fail "renders a storage server without storage.namespaces"
helm template t . "${base[@]}" --set runtime.storage.s3.bucket=ach-bucket --set 'storage.namespaces={ach-agent}' \
  >/dev/null 2>&1 && fail "renders a storage server without a region"

out=$(helm template t . "${base[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent,team-b}')
python3 - "$out" <<'PY'
import base64, json, sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
def of(kind, ns=None):
    return [d for d in docs if d["kind"] == kind and (ns is None or d["metadata"].get("namespace") == ns)]
dep, = of("Deployment")
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://ach-agent-storage:8333", env
assert env["ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"] == "ach-agent-storage-s3", env
assert env["ACH_AGENT_STORAGE_S3_BUCKET"] == "ach-bucket", env
for kind in ("StatefulSet", "Service", "Secret", "ConfigMap", "ServiceAccount", "AuthorizationPolicy"):
    got = sorted(d["metadata"]["namespace"] for d in of(kind) if d["metadata"]["name"].startswith("ach-agent-storage"))
    assert got == ["ach-agent", "team-b"], (kind, got)
tb, = of("StatefulSet", "team-b")
assert {e["name"]: e.get("value") for e in tb["spec"]["template"]["spec"]["containers"][0]["env"]}["MOUNT_PATH"] == "ach/team-b"
sts, = of("StatefulSet", "ach-agent")
pod = sts["spec"]["template"]["spec"]
assert sts["spec"]["replicas"] == 1 and sts["spec"]["serviceName"] == "ach-agent-storage"
assert pod["serviceAccountName"] == "ach-agent-storage"
assert sts["spec"]["template"]["metadata"]["labels"]["istio.io/dataplane-mode"] == "ambient"
assert pod["securityContext"]["runAsNonRoot"] and pod["securityContext"]["runAsUser"] == 1000
c = {x["name"]: x for x in pod["containers"]}
assert sorted(c) == ["reconcile", "server", "sync"], sorted(c)
for x in c.values():
    assert x["image"] == "chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8", x["image"]
    sc = x["securityContext"]
    assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
    e = {v["name"]: v for v in x["env"]}
    assert e["BUCKET"]["value"] == "ach-bucket" and e["AWS_REGION"]["value"] == "eu-west-1", e
    assert e["REMOTE_ENDPOINT"]["value"] == "https://s3.eu-west-1.amazonaws.com", e
    assert e["REMOTE_TYPE"]["value"] == "s3", e
    assert e["MOUNT_PATH"]["value"] == "ach/ach-agent", e
    assert "AWS_ACCESS_KEY_ID" not in e, "without credentialsSecret the default AWS chain (Pod Identity) applies"
args = c["server"]["args"]
for flag in ("-filer.remoteCacheEvictThreshold=0.8", "-s3.config=/etc/seaweedfs/identities.json",
             "-ip.bind=0.0.0.0", "-filer.allowUntrustedRemoteEndpoints"):
    assert flag in args, (flag, args)
assert {p["name"]: p["containerPort"] for p in c["server"]["ports"]} == {"s3": 8333, "metrics": 9327}
assert {p["name"]: p["containerPort"] for p in c["reconcile"]["ports"]} == {"reconcile": 9328}
assert sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "50Gi"
sec, = [d for d in of("Secret", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage-s3"]
data = {k: base64.b64decode(v).decode() for k, v in sec["data"].items()}
ident = json.loads(data["identities.json"])["identities"][0]
assert ident["credentials"][0] == {"accessKey": data["AWS_ACCESS_KEY_ID"], "secretKey": data["AWS_SECRET_ACCESS_KEY"]}
assert "Admin" not in ident["actions"], ident
svc, = [d for d in of("Service", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage"]
assert [p["port"] for p in svc["spec"]["ports"]] == [8333]
ap, = of("AuthorizationPolicy", "ach-agent")
rules = ap["spec"]["rules"]
assert rules[0]["from"][0]["source"]["principals"] == ["cluster.local/ns/ach-agent/sa/ach-agent-control*"], rules
assert rules[0]["to"][0]["operation"]["ports"] == ["8333"], rules
assert "from" not in rules[1] and rules[1]["to"][0]["operation"]["ports"] == ["9327", "9328"], rules
cm, = [d for d in of("ConfigMap", "ach-agent") if d["metadata"]["name"] == "ach-agent-storage-scripts"]
assert sorted(cm["data"]) == ["metrics.sh", "reconcile.sh", "sync.sh"]
print("storage ok")
PY

out=$(helm template t . "${base[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent}' \
  --set runtime.storage.s3.endpointUrl=http://minio.minio:9000 --set runtime.storage.s3.credentialsSecret=s3-keys)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
dep, = [d for d in docs if d["kind"] == "Deployment"]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://ach-agent-storage:8333", env
sts, = [d for d in docs if d["kind"] == "StatefulSet"]
for x in sts["spec"]["template"]["spec"]["containers"]:
    e = {v["name"]: v for v in x["env"]}
    assert e["REMOTE_ENDPOINT"]["value"] == "http://minio.minio:9000", e
    assert e["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {"name": "s3-keys", "key": "AWS_ACCESS_KEY_ID"}, e
    assert e["AWS_SESSION_TOKEN"]["valueFrom"]["secretKeyRef"]["optional"] is True, e
print("storage custom endpoint ok")
PY

out=$(helm template t . "${base[@]}" "${s3[@]}" --set storage.enabled=false \
  --set runtime.storage.s3.endpointUrl=http://minio.minio:9000 --set runtime.storage.s3.credentialsSecret=s3-keys)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
assert not [d for d in docs if d["kind"] == "StatefulSet"]
dep, = [d for d in docs if d["kind"] == "Deployment"]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://minio.minio:9000", env
assert env["ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"] == "s3-keys", env
print("storage disabled ok")
PY

gcs=(--set storage.remote.type=gcs --set storage.remote.gcs.bucket=ach-gcs --set storage.remote.gcs.project=ach-prod)
helm template t . "${base[@]}" "${gcs[@]}" --set storage.enabled=false >/dev/null 2>&1 && fail "renders gcs without the storage server"
helm template t . "${base[@]}" --set storage.remote.type=gcs --set storage.remote.gcs.project=ach-prod \
  --set 'storage.namespaces={ach-agent}' >/dev/null 2>&1 && fail "renders gcs without a bucket"
helm template t . "${base[@]}" --set storage.remote.type=gcs --set storage.remote.gcs.bucket=ach-gcs \
  --set 'storage.namespaces={ach-agent}' >/dev/null 2>&1 && fail "renders gcs without a project"
out=$(helm template t . "${base[@]}" "${gcs[@]}" --set 'storage.namespaces={ach-agent}' \
  --set 'storage.serviceAccount.annotations.iam\.gke\.io/gcp-service-account=ach-storage@ach-prod.iam.gserviceaccount.com')
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
dep, = [d for d in docs if d["kind"] == "Deployment"]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["ACH_AGENT_STORAGE_S3_ENDPOINT_URL"] == "http://ach-agent-storage:8333", env
assert env["ACH_AGENT_STORAGE_S3_BUCKET"] == "ach-gcs" and env["ACH_AGENT_STORAGE_S3_PREFIX"] == "ach", env
assert env["ACH_AGENT_STORAGE_S3_REGION"] == "us-east-1", env
assert env["ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"] == "ach-agent-storage-s3", env
sa, = [d for d in docs if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "ach-agent-storage"]
assert sa["metadata"]["annotations"] == {"iam.gke.io/gcp-service-account": "ach-storage@ach-prod.iam.gserviceaccount.com"}, sa
sts, = [d for d in docs if d["kind"] == "StatefulSet"]
pod = sts["spec"]["template"]["spec"]
assert "gcs-credentials" not in [v["name"] for v in pod["volumes"]]
for x in pod["containers"]:
    e = {v["name"]: v.get("value") for v in x["env"]}
    assert e["REMOTE_TYPE"] == "gcs" and e["BUCKET"] == "ach-gcs" and e["GCS_PROJECT"] == "ach-prod", e
    assert not {"REMOTE_ENDPOINT", "AWS_REGION", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS"} & set(e), e
print("storage gcs ok")
PY

out=$(helm template t . "${base[@]}" "${gcs[@]}" --set 'storage.namespaces={ach-agent}' \
  --set storage.remote.gcs.credentialsSecret=gcs-key)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
sts, = [d for d in docs if d["kind"] == "StatefulSet"]
pod = sts["spec"]["template"]["spec"]
vol, = [v for v in pod["volumes"] if v["name"] == "gcs-credentials"]
assert vol["secret"]["secretName"] == "gcs-key", vol
for x in pod["containers"]:
    e = {v["name"]: v.get("value") for v in x["env"]}
    assert e["GOOGLE_APPLICATION_CREDENTIALS"] == "/etc/gcs/credentials.json", e
    assert {"name": "gcs-credentials", "mountPath": "/etc/gcs", "readOnly": True} in x["volumeMounts"], x["volumeMounts"]
print("storage gcs key ok")
PY

on=(--set metrics.podMonitor.enabled=true --set metrics.prometheusRule.enabled=true --set metrics.dashboards.enabled=true)
out=$(helm template t . "${base[@]}" "${on[@]}" --set-string 'metrics.dashboards.annotations.grafana_folder=100 - Application - ACH Agents')
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
def of(kind): return [d for d in docs if d["kind"] == kind]
pm, = of("PodMonitor")
assert pm["spec"]["namespaceSelector"] == {"any": True}
assert pm["spec"]["selector"]["matchLabels"]["ach-agent.ackstorm.ai/component"] == "control"
assert pm["spec"]["podMetricsEndpoints"][0]["port"] == "http"
rule, = of("PrometheusRule")
alerts = [r["alert"] for g in rule["spec"]["groups"] for r in g["rules"]]
assert len(alerts) == 9 and "ACHAgentStartFailing" in alerts and "ACHAgentKeyRefreshFailing" in alerts, alerts
assert {"ACHAgentStorageDown", "ACHAgentStorageDiskHigh", "ACHAgentStorageSyncLagging"} <= set(alerts), alerts
assert "$labels.agent" in str(rule["spec"]), "alert templates must reach Prometheus verbatim"
cms = [d for d in of("ConfigMap") if d["metadata"]["labels"].get("grafana_dashboard") == "1"]
assert len(cms) == 3, len(cms)
for cm in cms:
    assert cm["metadata"]["annotations"]["grafana_folder"] == "100 - Application - ACH Agents"
    import json
    [json.loads(v) for v in cm["data"].values()]
print("observability ok")
PY
out=$(helm template t . "${base[@]}" "${on[@]}" "${s3[@]}" --set 'storage.namespaces={ach-agent,team-b}')
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
pms = {d["metadata"]["name"]: d for d in docs if d["kind"] == "PodMonitor"}
assert sorted(pms) == ["ach-agent-control", "ach-agent-storage"], sorted(pms)
spec = pms["ach-agent-storage"]["spec"]
assert spec["namespaceSelector"] == {"matchNames": ["ach-agent", "team-b"]}, spec
assert spec["selector"]["matchLabels"]["app.kubernetes.io/name"] == "ach-agent-storage", spec
assert [e["port"] for e in spec["podMetricsEndpoints"]] == ["metrics", "reconcile"], spec
print("storage observability ok")
PY
echo PASS
