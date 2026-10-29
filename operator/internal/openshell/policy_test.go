// SPDX-License-Identifier: MIT

package openshell

import (
	"errors"
	"slices"
	"strings"
	"testing"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func raw(s string) *apiextensionsv1.JSON { return &apiextensionsv1.JSON{Raw: []byte(s)} }

func TestBuildPolicy_ReservedFacadeRuleAndDefaults(t *testing.T) {
	p, err := BuildPolicy(nil, nil, "agent-r.ns.svc.cluster.local", 8081)
	if err != nil {
		t.Fatal(err)
	}
	rule, ok := p.NetworkPolicies["ach-agent-facades"]
	if !ok || rule.Endpoints[0].Host != "agent-r.ns.svc.cluster.local" || rule.Endpoints[0].Port != 8081 {
		t.Fatalf("facade rule = %#v", p.NetworkPolicies)
	}
	if len(rule.Binaries) != 3 {
		t.Fatalf("binaries = %#v", rule.Binaries)
	}
	if p.Landlock.Compatibility != "hard_requirement" {
		t.Fatalf("landlock = %#v", p.Landlock)
	}
	if !p.Filesystem.IncludeWorkdir || p.Version != 1 {
		t.Fatalf("policy = %#v", p)
	}
}

func TestBuildPolicy_AgentAddsEgressAndOverridesProfileRuleByName(t *testing.T) {
	profile := raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"pypi.org","port":443}]}}}`)
	agent := raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"files.pythonhosted.org","port":443}]},
	  "serper":{"name":"serper","endpoints":[{"host":"google.serper.dev","port":443}]}},
	  "landlock":{"compatibility":"best_effort"}}`)
	p, err := BuildPolicy(profile, agent, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if p.NetworkPolicies["pypi"].Endpoints[0].Host != "files.pythonhosted.org" {
		t.Fatalf("agent must win by name: %#v", p.NetworkPolicies["pypi"])
	}
	if _, ok := p.NetworkPolicies["serper"]; !ok {
		t.Fatal("agent egress dropped")
	}
	if p.Landlock.Compatibility != "best_effort" {
		t.Fatal("agent may set landlock")
	}
}

func TestBuildPolicy_ProfileRulesSurviveWithoutAnAgentPolicy(t *testing.T) {
	profile := raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"pypi.org","port":443}]}},"landlock":{"compatibility":"best_effort"}}`)
	p, err := BuildPolicy(profile, nil, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := p.NetworkPolicies["pypi"]; !ok || p.Landlock.Compatibility != "best_effort" {
		t.Fatalf("profile policy lost: %#v", p)
	}
}

func TestBuildPolicy_FilesystemListsConcatenate(t *testing.T) {
	profile := raw(`{"filesystem":{"read_only":["/data"]}}`)
	agent := raw(`{"filesystem":{"read_write":["/scratch"]}}`)
	p, err := BuildPolicy(profile, agent, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(p.Filesystem.ReadOnly, "/data") || !slices.Contains(p.Filesystem.ReadWrite, "/scratch") ||
		!slices.Contains(p.Filesystem.ReadWrite, "/tmp") || !slices.Contains(p.Filesystem.ReadOnly, "/usr") {
		t.Fatalf("filesystem = %#v", p.Filesystem)
	}
	for _, list := range [][]string{p.Filesystem.ReadOnly, p.Filesystem.ReadWrite} {
		seen := map[string]bool{}
		for _, e := range list {
			if seen[e] {
				t.Fatalf("duplicate %q in %v", e, list)
			}
			seen[e] = true
		}
	}
}

func TestBuildPolicy_Rejections(t *testing.T) {
	for name, tc := range map[string]struct{ doc, why string }{
		"unknown field":         {`{"network_policy":{}}`, "unknown field"},
		"reserved rule name":    {`{"network_policies":{"ach-agent-x":{"name":"ach-agent-x"}}}`, "reserved prefix"},
		"writable runtime bin":  {`{"filesystem":{"read_write":["/usr/local/bin"]}}`, "writable"},
		"writable opencode dir": {`{"filesystem":{"read_write":["/opt/opencode/node_modules"]}}`, "writable"},
		"writable parent":       {`{"filesystem":{"read_write":["/opt"]}}`, "writable"},
		"writable root":         {`{"filesystem":{"read_write":["/"]}}`, "writable"},
		"writable via dotdot":   {`{"filesystem":{"read_write":["/tmp/../usr/local/bin"]}}`, "writable"},
	} {
		t.Run(name, func(t *testing.T) {
			for who, args := range map[string][2]*apiextensionsv1.JSON{"agent": {nil, raw(tc.doc)}, "profile": {raw(tc.doc), nil}} {
				_, err := BuildPolicy(args[0], args[1], "f", 8081)
				if !errors.Is(err, ErrPolicyRejected) || !strings.Contains(err.Error(), tc.why) {
					t.Fatalf("%s policy: err = %v, want a rejection mentioning %q", who, err, tc.why)
				}
			}
		})
	}
}

func TestResources_MapsCpuMemory(t *testing.T) {
	m, err := Resources(&v1.SandboxResources{CPU: "2", Memory: "4Gi"})
	if err != nil || m["cpu"] != "2" || m["memory"] != "4Gi" || len(m) != 2 {
		t.Fatalf("m=%v err=%v", m, err)
	}
	if m, err := Resources(nil); m != nil || err != nil {
		t.Fatalf("nil resources = %v, %v", m, err)
	}
	if m, err := Resources(&v1.SandboxResources{}); m != nil || err != nil {
		t.Fatalf("empty resources = %v, %v", m, err)
	}
}
