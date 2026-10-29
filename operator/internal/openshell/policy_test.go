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
	if p.Filesystem.IncludeWorkdir || p.Version != 1 {
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
		"unknown field":          {`{"network_policy":{}}`, "unknown field"},
		"reserved rule name":     {`{"network_policies":{"ach-agent-x":{"name":"ach-agent-x"}}}`, "reserved prefix"},
		"writable runtime bin":   {`{"filesystem":{"read_write":["/usr/local/bin"]}}`, "writable"},
		"writable opencode dir":  {`{"filesystem":{"read_write":["/opt/opencode/node_modules"]}}`, "writable"},
		"writable parent":        {`{"filesystem":{"read_write":["/opt"]}}`, "writable"},
		"writable root":          {`{"filesystem":{"read_write":["/"]}}`, "writable"},
		"writable via dotdot":    {`{"filesystem":{"read_write":["/tmp/../usr/local/bin"]}}`, "writable"},
		"writable runtime code":  {`{"filesystem":{"read_write":["/work/src"]}}`, "writable"},
		"writable work":          {`{"filesystem":{"read_write":["/work"]}}`, "writable"},
		"writable stdlib":        {`{"filesystem":{"read_write":["/usr/local/lib/python3.13"]}}`, "writable"},
		"relative read_write":    {`{"filesystem":{"read_write":["usr/local/bin"]}}`, "absolute"},
		"dotdot read_write":      {`{"filesystem":{"read_write":["../usr/local/bin"]}}`, "absolute"},
		"relative read_only":     {`{"filesystem":{"read_only":["data"]}}`, "absolute"},
		"reserved name field":    {`{"network_policies":{"x":{"name":"ach-agent-facades"}}}`, "reserved prefix"},
		"reserved name mixed":    {`{"network_policies":{"x":{"name":"ACH-Agent-facades"}}}`, "reserved prefix"},
		"name differs from key":  {`{"network_policies":{"x":{"name":"y"}}}`, "differs from its key"},
		"facade exact host":      {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"F.","port":8081}]}}}`, "facade"},
		"facade host glob":       {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"f*","port":8081}]}}}`, "facade"},
		"facade wildcard host":   {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"**","port":8081}]}}}`, "facade"},
		"facade ports list":      {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"f","ports":[443,8081]}]}}}`, "facade"},
		"facade allowed_ips":     {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"example.com","port":8081,"allowed_ips":["10.0.0.0/8"]}]}}}`, "facade"},
		"reserved middleware":    {`{"network_middlewares":{"ach-agent-m":{"name":"m","middleware":"x"}}}`, "reserved prefix"},
		"middleware on facade":   {`{"network_middlewares":{"m":{"name":"m","middleware":"x","endpoints":{"include":["f"]}}}}`, "facade"},
		"internal pod dns":       {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"agent-foo-0.agent-foo.ns.svc.cluster.local","port":443}]}}}`, "cluster-internal"},
		"internal svc":           {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"agent-foo.ns.svc.","port":443}]}}}`, "cluster-internal"},
		"internal cluster.local": {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"A.Cluster.Local","port":443}]}}}`, "cluster-internal"},
		"internal short name":    {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"agent-foo","port":443}]}}}`, "cluster-internal"},
		"internal ip host":       {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"10.1.2.3","port":443}]}}}`, "cluster-internal"},
		"cgnat ip host":          {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"100.64.1.1","port":443}]}}}`, "cluster-internal"},
		"loopback v6":            {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"::1","port":443}]}}}`, "cluster-internal"},
		"ula v6":                 {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"fd00::1","port":443}]}}}`, "cluster-internal"},
		"link-local allowed_ips": {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"example.com","port":443,"allowed_ips":["169.254.169.254/32"]}]}}}`, "cluster-internal"},
		"rfc1918 allowed_ips":    {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"example.com","port":443,"allowed_ips":["192.168.0.0/16"]}]}}}`, "cluster-internal"},
		"v6 link-local allowed":  {`{"network_policies":{"x":{"name":"x","endpoints":[{"host":"example.com","port":443,"allowed_ips":["fe80::/10"]}]}}}`, "cluster-internal"},
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

func TestBuildPolicy_UnrelatedEgressIsKept(t *testing.T) {
	doc := raw(`{"network_policies":{"a":{"name":"a","endpoints":[{"host":"pypi.org","port":443},{"host":"*.example.com","port":8081},{"host":"example.com","port":8081,"allowed_ips":["10.0.0.0/8"]}]}}}`)
	if _, err := BuildPolicy(nil, doc, "f.ns.svc", 8081); err == nil {
		t.Fatal("allowed_ips on the facade port must be rejected")
	}
	doc = raw(`{"network_policies":{"a":{"name":"a","endpoints":[{"host":"pypi.org","port":443},{"host":"*.example.com","port":8081},{"host":"f.other.example","port":8081}]}}}`)
	if _, err := BuildPolicy(nil, doc, "f.ns.svc", 8081); err != nil {
		t.Fatalf("unrelated egress rejected: %v", err)
	}
	doc = raw(`{"network_policies":{"a":{"name":"a","endpoints":[{"host":"203.0.113.9","port":443,"allowed_ips":["203.0.113.0/24","2001:db8::/32"]},{"host":"api.example.com","port":443}]}}}`)
	if _, err := BuildPolicy(nil, doc, "f.ns.svc", 8081); err != nil {
		t.Fatalf("external egress rejected: %v", err)
	}
}

func TestBuildPolicy_RuntimeCodeIsReadOnly(t *testing.T) {
	p, err := BuildPolicy(nil, nil, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(p.Filesystem.ReadOnly, "/work") || slices.Contains(p.Filesystem.ReadWrite, "/work") {
		t.Fatalf("filesystem = %#v", p.Filesystem)
	}
	if !slices.Contains(p.Filesystem.ReadWrite, "/workspace") {
		t.Fatalf("/workspace must be listed explicitly: %#v", p.Filesystem)
	}
}

func TestResources_MapsCpuMemory(t *testing.T) {
	m, err := Resources(&v1.SandboxResources{CPU: "2", Memory: "4Gi", EphemeralStorage: "10Gi"})
	if err != nil || m["cpu"] != "2" || m["memory"] != "4Gi" || m["ephemeral-storage"] != "10Gi" || len(m) != 3 {
		t.Fatalf("m=%v err=%v", m, err)
	}
	if m, err := Resources(nil); m != nil || err != nil {
		t.Fatalf("nil resources = %v, %v", m, err)
	}
	if m, err := Resources(&v1.SandboxResources{}); m != nil || err != nil {
		t.Fatalf("empty resources = %v, %v", m, err)
	}
}

// The engine that calls the model facade is the native opencode.exe that
// node_modules/.bin/opencode2 symlinks to; leaving it out denies every model call.
func TestBuildPolicy_FacadeRuleBindsExactlyTheRuntimeBinaries(t *testing.T) {
	p, err := BuildPolicy(nil, nil, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, b := range p.NetworkPolicies["ach-agent-facades"].Binaries {
		got = append(got, b.Path)
	}
	want := []string{
		"/usr/local/bin/python3.13",
		"/usr/local/bin/node",
		"/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe",
	}
	if !slices.Equal(got, want) {
		t.Fatalf("binaries = %v, want %v", got, want)
	}
}
