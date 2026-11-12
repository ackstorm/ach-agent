// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"os"
	"strings"
	"testing"
)

func fixtures(t *testing.T) string {
	t.Helper()
	var docs []string
	for _, name := range []string{"profile.yaml", "standalone-agent.yaml"} {
		raw, err := os.ReadFile("../../../test/e2e/fixtures/" + name)
		if err != nil {
			t.Fatal(err)
		}
		docs = append(docs, strings.ReplaceAll(string(raw), "${NAME}", "demo"))
	}
	return strings.Join(docs, "\n---\n")
}

func TestValidate(t *testing.T) {
	good := fixtures(t)
	for name, tc := range map[string]struct{ input, want string }{
		"fixtures pass":            {good, ""},
		"grace below the drain":    {strings.Replace(good, "terminationGracePeriodSeconds: 60", "terminationGracePeriodSeconds: 10", 1), "terminationGracePeriodSeconds 10 is below"},
		"unknown field":            {strings.Replace(good, "capabilityServer: standalone", "capabilityServer: standalone\n  capabilty: x", 1), "unknown field"},
		"profile not in the files": {strings.Replace(good, "profileRef: {name: e2e}", "profileRef: {name: other}", 1), `AgentProfile "other" of namespace "ach-agent-e2e" is not in the files`},
	} {
		var out bytes.Buffer
		err := runValidate([]string{"-f", "-"}, strings.NewReader(tc.input), &out)
		switch {
		case tc.want == "" && err != nil:
			t.Errorf("%s: %v", name, err)
		case tc.want == "" && out.String() != "Agent demo: ok\n":
			t.Errorf("%s: output %q", name, out.String())
		case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
			t.Errorf("%s: got %v, want %q", name, err, tc.want)
		}
	}
}
