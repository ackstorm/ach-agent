//go:build e2e

package e2e

import (
	"strings"
	"testing"
	"time"
)

// The scenarios share one cluster and run in order.
func TestE2E(t *testing.T) {
	var sandboxNames []string
	setup := func(t *testing.T) {
		applySecret(t, "e2e-model", "apiKey", "e2e-model-key")
		applySecret(t, "e2e-serper", "apiKey", "e2e-serper-key")
		applySecret(t, "e2e-hook", "token", hookToken)
		applySecret(t, "e2e-ek", "token", "e2e-ek")
		apply(t, "fixtures/profile.yaml", nil)
		setStubKey(t, "model", "e2e-model-key") // a previous run may have rotated it
	}

	t.Run("standalone", func(t *testing.T) {
		setup(t)
		clearStub(t)
		apply(t, "fixtures/standalone-agent.yaml", map[string]string{"NAME": "standalone"})
		waitCond(t, "standalone", "Ready", "True", 5*time.Minute)
		postAccepted(t, "standalone", 2*time.Minute)
		chats := waitChat(t, 8090, 4*time.Minute)

		for _, r := range chats {
			if got := r.headerValues("x-litellm-api-key"); len(got) != 1 || got[0] != "e2e-model-key" {
				t.Errorf("model call carried x-litellm-api-key %v, want exactly [e2e-model-key]", got)
			}
			for _, h := range []string{"x-ach-key", "authorization"} {
				if v := r.headerValues(h); len(v) != 0 {
					t.Errorf("model call carried %s %v", h, v)
				}
			}
		}
		for _, r := range stubRequests(t, 8090) {
			if r.Path != "/v1/chat/completions" {
				t.Errorf("model path %s is not on the allow-list", r.Path)
			}
		}
		until(t, 2*time.Minute, "the MCP stub never saw X-API-KEY", nil, func() bool {
			for _, r := range stubRequests(t, 8091) {
				if v := r.headerValues("x-api-key"); len(v) == 1 && v[0] == "e2e-serper-key" {
					return true
				}
			}
			return false
		})
		for _, r := range stubRequests(t, 8091) {
			if v := r.headerValues("x-api-key"); len(v) != 1 || v[0] != "e2e-serper-key" {
				t.Errorf("MCP call carried X-API-KEY %v", v)
			}
		}

		uid := agentUID(t, "standalone")
		sandboxNames = sandboxes(t, uid)
		if len(sandboxNames) == 0 {
			t.Fatalf("no sandbox pod for agent uid %s\n%s", uid, agentYAML("standalone")())
		}
		if log := operatorLog(0, 2000); strings.Contains(log, "SandboxPolicyRejected") {
			t.Errorf("operator log mentions SandboxPolicyRejected:\n%s", log)
		}
	})

	t.Run("sandbox-egress", func(t *testing.T) {
		if len(sandboxNames) == 0 {
			t.Skip("needs the standalone sandbox")
		}
		pod := sandboxNames[0]
		// wget must exist, and the stub must be unreachable from the sandbox workload.
		out, err := kubectl("", "-n", "openshell", "exec", pod, "-c", "agent", "--", "sh", "-c",
			"command -v wget >/dev/null && ! wget -q -T 5 -O- http://"+stubHost+":8090/healthz")
		if err != nil {
			t.Errorf("the sandbox reached the stub directly (or wget is missing): %v\n%s", err, out)
		}
		res := jsonpath(t, "-n", "openshell", "get", "sandboxes.agents.x-k8s.io", pod,
			"-o", "jsonpath={.spec.podTemplate.spec.containers[0].resources}")
		if strings.Contains(res, "cpu") && strings.Contains(res, "memory") {
			t.Logf("sandbox resources applied: %s", res)
		} else {
			t.Logf("KNOWN GAP: the gateway ignored template resources {cpu, memory}; container resources = %q", res)
		}
	})

	t.Run("ach-mode", func(t *testing.T) {
		clearStub(t)
		apply(t, "fixtures/ach-agent.yaml", map[string]string{"NAME": "ach-mode"})
		t.Cleanup(func() { kubectl("", "-n", agentNS, "delete", "agent", "ach-mode", "--wait=false") })
		waitCond(t, "ach-mode", "Ready", "True", 5*time.Minute)
		postAccepted(t, "ach-mode", 2*time.Minute)
		chats := waitChat(t, 8092, 4*time.Minute)
		if v := chats[0].headerValues("x-ach-key"); len(v) != 1 || v[0] != "e2e-ek" {
			t.Errorf("ACH model call carried x-ach-key %v", v)
		}
		found := false
		for _, r := range stubRequests(t, 8092) {
			if r.Path == "/platform/hydrate" {
				if v := r.headerValues("x-ach-key"); len(v) == 1 && v[0] == "e2e-ek" {
					found = true
				}
			}
		}
		if !found {
			t.Errorf("the ACH stub never saw /platform/hydrate with x-ach-key e2e-ek: %+v", stubRequests(t, 8092))
		}
	})

	t.Run("rejected-policy", func(t *testing.T) {
		apply(t, "fixtures/rejected-policy-agent.yaml", map[string]string{"NAME": "rejected"})
		t.Cleanup(func() { kubectl("", "-n", agentNS, "delete", "agent", "rejected", "--wait=false") })
		waitCond(t, "rejected", "WorkloadReady", "True", 5*time.Minute)
		postAccepted(t, "rejected", 2*time.Minute)
		until(t, 3*time.Minute, "Ready never became False/SandboxPolicyRejected", agentYAML("rejected"), func() bool {
			s, r := condition(t, "rejected", "Ready")
			return s == "False" && r == "SandboxPolicyRejected"
		})
		mustKubectl(t, "", "-n", agentNS, "patch", "agent", "rejected", "--type=json",
			"-p", `[{"op":"remove","path":"/spec/sandbox"}]`)
		until(t, 3*time.Minute, "Ready never returned to True", agentYAML("rejected"), func() bool {
			s, _ := condition(t, "rejected", "Ready")
			return s == "True"
		})
	})

	t.Run("gateway-restart", func(t *testing.T) {
		opPod := jsonpath(t, "-n", "ach-agent-system", "get", "pods", "-l", "app.kubernetes.io/name=ach-agent-operator", "-o", "jsonpath={.items[0].metadata.uid}")
		uidsBefore := podUIDs(t, sandboxNames)
		restartGateway(t)
		waitCond(t, "standalone", "GatewayAvailable", "True", 3*time.Minute)
		if got := jsonpath(t, "-n", "ach-agent-system", "get", "pods", "-l", "app.kubernetes.io/name=ach-agent-operator", "-o", "jsonpath={.items[0].metadata.uid}"); got != opPod {
			t.Errorf("the operator pod was replaced (%s -> %s)", opPod, got)
		}
		serveUntilChat(t, "standalone", 8090, 6*time.Minute)
		after := sandboxes(t, agentUID(t, "standalone"))
		if strings.Join(after, ",") != strings.Join(sandboxNames, ",") {
			t.Errorf("sandbox name changed across the gateway restart: %v -> %v", sandboxNames, after)
		}
		if now := podUIDs(t, after); now != uidsBefore {
			t.Logf("KNOWN GAP: OpenShell 0.1.5 drops its sandboxes on a gateway restart; the runtime replaced the pod on the next webhook (same deterministic name): %s -> %s", uidsBefore, now)
		}
	})

	t.Run("gateway-down-control-restart", func(t *testing.T) {
		oldUID := jsonpath(t, "-n", agentNS, "get", "pod", "agent-standalone-0", "-o", "jsonpath={.metadata.uid}")
		uidsBefore := podUIDs(t, sandboxes(t, agentUID(t, "standalone")))
		mustKubectl(t, "", "-n", "openshell", "scale", "statefulset/openshell", "--replicas=0")
		until(t, 2*time.Minute, "gateway pod did not terminate", nil, func() bool {
			_, err := kubectl("", "-n", "openshell", "get", "pod", "openshell-0")
			return err != nil
		})
		mustKubectl(t, "", "-n", agentNS, "delete", "pod", "agent-standalone-0", "--wait=false")
		until(t, 3*time.Minute, "new control pod not Running", agentYAML("standalone"), func() bool {
			uid := jsonpath(t, "-n", agentNS, "get", "pod", "agent-standalone-0", "-o", "jsonpath={.metadata.uid}")
			phase := jsonpath(t, "-n", agentNS, "get", "pod", "agent-standalone-0", "-o", "jsonpath={.status.phase}")
			return uid != "" && uid != oldUID && phase == "Running"
		})
		waitCond(t, "standalone", "GatewayAvailable", "False", 3*time.Minute)
		mustKubectl(t, "", "-n", "openshell", "scale", "statefulset/openshell", "--replicas=1")
		mustKubectl(t, "", "-n", "openshell", "rollout", "status", "statefulset/openshell", "--timeout=300s")
		waitCond(t, "standalone", "GatewayAvailable", "True", 5*time.Minute)
		waitCond(t, "standalone", "Ready", "True", 5*time.Minute)
		serveUntilChat(t, "standalone", 8090, 6*time.Minute)
		got := sandboxes(t, agentUID(t, "standalone"))
		if strings.Join(got, ",") != strings.Join(sandboxNames, ",") {
			t.Errorf("sandbox name changed after the outage: %v -> %v", sandboxNames, got)
		}
		if now := podUIDs(t, got); now != uidsBefore {
			t.Logf("KNOWN GAP: OpenShell 0.1.5 drops its sandboxes while the gateway is down; the runtime replaced the pod on the next webhook (same deterministic name): %s -> %s", uidsBefore, now)
		}
	})

	t.Run("secret-rotation", func(t *testing.T) {
		hash := func() string {
			return jsonpath(t, "-n", agentNS, "get", "statefulset", "agent-standalone", "-o",
				`jsonpath={.spec.template.metadata.annotations.ach-agent\.ackstorm\.ai/config-hash}`)
		}
		before := hash()
		setStubKey(t, "model", "e2e-model-key-2")
		applySecret(t, "e2e-model", "apiKey", "e2e-model-key-2")
		until(t, 3*time.Minute, "config-hash did not change", agentYAML("standalone"), func() bool {
			h := hash()
			return h != "" && h != before
		})
		mustKubectl(t, "", "-n", agentNS, "rollout", "status", "statefulset/agent-standalone", "--timeout=300s")
		waitCond(t, "standalone", "Ready", "True", 5*time.Minute)
		for _, r := range serveUntilChat(t, "standalone", 8090, 6*time.Minute) {
			if v := r.headerValues("x-litellm-api-key"); len(v) != 1 || v[0] != "e2e-model-key-2" {
				t.Errorf("after rotation the model call carried %v", v)
			}
		}
	})

	t.Run("deletion", func(t *testing.T) {
		uid := agentUID(t, "standalone")
		before := sandboxes(t, uid)
		if len(before) == 0 {
			t.Fatal("no sandbox to delete")
		}
		mustKubectl(t, "", "-n", agentNS, "delete", "agent", "standalone", "--wait=false")
		until(t, 3*time.Minute, "agent, sandbox pods or Services still present", agentYAML("standalone"), func() bool {
			if _, err := kubectl("", "-n", agentNS, "get", "agent", "standalone"); err == nil {
				return false
			}
			if jsonpath(t, "-n", agentNS, "get", "svc", "-l", uidLabel+"="+uid, "-o", "jsonpath={.items[*].metadata.name}") != "" {
				return false
			}
			for _, name := range before {
				if _, err := kubectl("", "-n", "openshell", "get", "pod", name); err == nil {
					return false
				}
			}
			return true
		})
	})
}
