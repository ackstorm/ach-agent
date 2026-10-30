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
assert c["image"].endswith(":0.1.3"), c["image"]
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
print("render ok")
PY
for f in dashboards/*.json; do python3 -m json.tool "$f" >/dev/null || fail "$f is not valid JSON"; done

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
assert len(alerts) == 5 and "ACHAgentStartFailing" in alerts, alerts
assert "$labels.agent" in str(rule["spec"]), "alert templates must reach Prometheus verbatim"
cms = [d for d in of("ConfigMap") if d["metadata"]["labels"].get("grafana_dashboard") == "1"]
assert len(cms) == 3, len(cms)
for cm in cms:
    assert cm["metadata"]["annotations"]["grafana_folder"] == "100 - Application - ACH Agents"
    import json
    [json.loads(v) for v in cm["data"].values()]
print("observability ok")
PY
echo PASS
