//go:build e2e

package e2e

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

const (
	kubeContext = "kind-ach-agent-e2e"
	agentNS     = "ach-agent-e2e"
	stubHost    = "e2e-stub." + agentNS + ".svc.cluster.local"
	uidLabel    = "runtime.ach-agent.ackstorm.ai/agent-uid"
	hookToken   = "e2e-hook-token"
)

// kubectl runs kubectl against the e2e cluster only.
func kubectl(stdin string, args ...string) (string, error) {
	cmd := exec.Command("kubectl", append([]string{"--context", kubeContext}, args...)...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func mustKubectl(t *testing.T, stdin string, args ...string) string {
	t.Helper()
	out, err := kubectl(stdin, args...)
	if err != nil {
		t.Fatalf("kubectl %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return out
}

// apply applies a fixture with ${VAR} substitution.
func apply(t *testing.T, path string, vars map[string]string) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	doc := os.Expand(string(raw), func(k string) string { return vars[k] })
	mustKubectl(t, doc, "apply", "-f", "-")
}

func applySecret(t *testing.T, name, key, value string) {
	t.Helper()
	doc := mustKubectl(t, "", "-n", agentNS, "create", "secret", "generic", name,
		"--from-literal="+key+"="+value, "--dry-run=client", "-o", "yaml")
	mustKubectl(t, doc, "-n", agentNS, "apply", "-f", "-")
}

func getJSON(t *testing.T, args ...string) map[string]any {
	t.Helper()
	out, err := kubectl("", append(args, "-o", "json")...)
	if err != nil {
		return nil
	}
	var m map[string]any
	if json.Unmarshal([]byte(out), &m) != nil {
		return nil
	}
	return m
}

func jsonpath(t *testing.T, args ...string) string {
	t.Helper()
	out, _ := kubectl("", args...)
	return strings.TrimSpace(out)
}

// until polls f every 3s up to timeout; on timeout it fails with msg and the dump output.
func until(t *testing.T, timeout time.Duration, msg string, dump func() string, f func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		if f() {
			return
		}
		if time.Now().After(deadline) {
			d := ""
			if dump != nil {
				d = dump()
			}
			t.Fatalf("timeout after %s: %s\n%s", timeout, msg, d)
		}
		time.Sleep(3 * time.Second)
	}
}

func agentYAML(name string) func() string {
	return func() string {
		out, _ := kubectl("", "-n", agentNS, "get", "agent", name, "-o", "yaml")
		return out + "\n--- operator log ---\n" + operatorLog(0, 80)
	}
}

// condition returns the status and reason of an Agent condition ("" when absent).
func condition(t *testing.T, agent, typ string) (status, reason string) {
	t.Helper()
	m := getJSON(t, "-n", agentNS, "get", "agent", agent)
	if m == nil {
		return "", ""
	}
	st, _ := m["status"].(map[string]any)
	conds, _ := st["conditions"].([]any)
	for _, c := range conds {
		cm := c.(map[string]any)
		if cm["type"] == typ {
			return fmt.Sprint(cm["status"]), fmt.Sprint(cm["reason"])
		}
	}
	return "", ""
}

func waitCond(t *testing.T, agent, typ, want string, timeout time.Duration) {
	t.Helper()
	until(t, timeout, fmt.Sprintf("agent %s condition %s=%s", agent, typ, want), agentYAML(agent), func() bool {
		s, _ := condition(t, agent, typ)
		return s == want
	})
}

func agentUID(t *testing.T, agent string) string {
	t.Helper()
	return jsonpath(t, "-n", agentNS, "get", "agent", agent, "-o", "jsonpath={.metadata.uid}")
}

// sandboxes lists the OpenShell sandbox pods of one Agent. A sandbox pod carries no Agent label; the
// operator labels the per-sandbox ExternalName Service, which has the same name as the pod.
func sandboxes(t *testing.T, uid string) []string {
	t.Helper()
	svcs := strings.Fields(jsonpath(t, "-n", agentNS, "get", "svc", "-l", uidLabel+"="+uid, "-o", "jsonpath={.items[*].metadata.name}"))
	var pods []string
	for _, name := range svcs {
		if out, err := kubectl("", "-n", "openshell", "get", "pod", name, "-o", "name"); err == nil && strings.TrimSpace(out) != "" {
			pods = append(pods, name)
		}
	}
	return pods
}

func operatorLog(_ int, tail int) string {
	out, _ := kubectl("", "-n", "ach-agent-system", "logs", "deploy/ach-agent-operator", fmt.Sprintf("--tail=%d", tail))
	return out
}

// stubExec runs a python snippet inside the stub pod (it can reach every Agent Service in the namespace).
func stubExec(t *testing.T, code string) (string, error) {
	t.Helper()
	return kubectl("", "-n", agentNS, "exec", "deploy/e2e-stub", "--", "python", "-c", code)
}

type recorded struct {
	Method     string            `json:"method"`
	Path       string            `json:"path"`
	Headers    map[string]string `json:"headers"`
	RawHeaders [][2]string       `json:"raw_headers"`
	Body       string            `json:"body"`
}

// stubRequests returns what the stub received on one port (8090 model, 8091 MCP, 8092 ACH, 8093 exchange).
func stubRequests(t *testing.T, port int) []recorded {
	t.Helper()
	out, err := kubectl("", "get", "--raw", fmt.Sprintf("/api/v1/namespaces/%s/services/e2e-stub:8090/proxy/_requests?port=%d", agentNS, port))
	if err != nil {
		t.Fatalf("stub requests: %v\n%s", err, out)
	}
	var all, r []recorded
	if err := json.Unmarshal([]byte(out), &all); err != nil {
		t.Fatalf("stub requests decode: %v\n%s", err, out)
	}
	for _, q := range all {
		if q.Path != "/healthz" { // readiness probes
			r = append(r, q)
		}
	}
	return r
}

func clearStub(t *testing.T) {
	t.Helper()
	if out, err := stubExec(t, "import urllib.request as u;u.urlopen(u.Request('http://127.0.0.1:8090/_requests',method='DELETE'))"); err != nil {
		t.Fatalf("clear stub: %v\n%s", err, out)
	}
}

func setStubKey(t *testing.T, kind, value string) {
	t.Helper()
	code := fmt.Sprintf("import urllib.request as u,json;u.urlopen(u.Request('http://127.0.0.1:8090/_creds',data=json.dumps({%q:%q}).encode(),method='PUT'))", kind, value)
	if out, err := stubExec(t, code); err != nil {
		t.Fatalf("set stub key: %v\n%s", err, out)
	}
}

var postSeq int

// postWebhook posts to the Agent's channel through its -http Service and returns the status code.
func postWebhook(t *testing.T, agent string) int {
	t.Helper()
	postSeq++
	svc := "agent-" + agent + "-http"
	code := fmt.Sprintf(`import urllib.request as u,urllib.error as e
r=u.Request('http://%s.%s.svc.cluster.local:8080/channels/hook/events',data=b'{"ask":"hi"}',method='POST',headers={'X-E2E-Token':%q,'X-Request-Id':'e2e-%d-%d','Content-Type':'application/json'})
try:
    print(u.urlopen(r,timeout=20).status)
except e.HTTPError as x:
    print(x.code)`, svc, agentNS, hookToken, time.Now().UnixNano(), postSeq)
	out, err := stubExec(t, code)
	if err != nil {
		t.Fatalf("post webhook: %v\n%s", err, out)
	}
	var status int
	fmt.Sscanf(strings.TrimSpace(out), "%d", &status)
	return status
}

// restartGateway restarts the OpenShell gateway and waits for it.
func restartGateway(t *testing.T) {
	t.Helper()
	mustKubectl(t, "", "-n", "openshell", "rollout", "restart", "statefulset/openshell")
	mustKubectl(t, "", "-n", "openshell", "rollout", "status", "statefulset/openshell", "--timeout=300s")
}

// postAccepted posts until the channel answers 202 (the control unit may still be starting).
func postAccepted(t *testing.T, agent string, timeout time.Duration) {
	t.Helper()
	var last int
	until(t, timeout, "webhook never answered 202 for "+agent, agentYAML(agent), func() bool {
		last = postWebhook(t, agent)
		return last == 202
	})
}

// chatRequests returns the model calls the stub received on one port (8090 standalone, 8092 ACH).
func chatRequests(t *testing.T, port int) []recorded {
	t.Helper()
	var out []recorded
	for _, r := range stubRequests(t, port) {
		if strings.HasSuffix(r.Path, "/chat/completions") {
			out = append(out, r)
		}
	}
	return out
}

func waitChat(t *testing.T, port int, timeout time.Duration) []recorded {
	t.Helper()
	var got []recorded
	until(t, timeout, fmt.Sprintf("no model call reached the stub on %d", port), func() string { return operatorLog(0, 40) }, func() bool {
		got = chatRequests(t, port)
		return len(got) > 0
	})
	return got
}

// headerValues returns every value of one header, case-insensitively, from the raw request.
func (r recorded) headerValues(name string) []string {
	var v []string
	for _, h := range r.RawHeaders {
		if strings.EqualFold(h[0], name) {
			v = append(v, h[1])
		}
	}
	return v
}

// serveUntilChat posts to the Agent every 45s until the model stub sees a call: a post that lands on a
// sandbox the gateway has just lost is dropped, and the runtime replaces the sandbox on the next one.
func serveUntilChat(t *testing.T, agent string, port int, timeout time.Duration) []recorded {
	t.Helper()
	clearStub(t)
	var got []recorded
	next := time.Time{}
	until(t, timeout, fmt.Sprintf("no model call reached the stub on %d after repeated webhooks", port), agentYAML(agent), func() bool {
		if got = chatRequests(t, port); len(got) > 0 {
			return true
		}
		if time.Now().After(next) {
			postWebhook(t, agent)
			next = time.Now().Add(45 * time.Second)
		}
		return false
	})
	return got
}

func podUIDs(t *testing.T, names []string) string {
	t.Helper()
	var out []string
	for _, n := range names {
		out = append(out, n+"="+jsonpath(t, "-n", "openshell", "get", "pod", n, "-o", "jsonpath={.metadata.uid}"))
	}
	return strings.Join(out, ",")
}

// facadeToken is the Workspace facade token the runtime derives (harness/tokens.py
// facade_token): <claim>.<hex HMAC-SHA256(K, "facade:<claim>")>, K = the agent key Secret's value.
func facadeToken(t *testing.T, agentUID, claim string) string {
	t.Helper()
	raw := mustKubectl(t, "", "-n", agentNS, "get", "secret", "ach-agent-key-"+agentUID, "-o", "jsonpath={.data.key}")
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(raw))
	if err != nil {
		t.Fatalf("decode agent key: %v", err)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("facade:" + claim))
	return claim + "." + hex.EncodeToString(mac.Sum(nil))
}

type loadSummary struct {
	ModelOK     int      `json:"model_ok"`
	ModelFailed int      `json:"model_failed"`
	McpOK       int      `json:"mcp_ok"`
	McpFailed   int      `json:"mcp_failed"`
	StreamOK    bool     `json:"stream_ok"`
	StreamError string   `json:"stream_error"`
	Errors      []string `json:"errors"`
}
