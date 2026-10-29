// SPDX-License-Identifier: MIT

package render

import (
	"encoding/json"
	"strings"
	"testing"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func standaloneFixture() (v1.AgentProfile, v1.Agent) {
	p, a := minimalFixture()
	a.Spec.CapabilityServer = "standalone"
	a.Spec.Standalone = &v1.StandaloneSpec{
		Model: v1.StandaloneModelSpec{
			Name: "anthropic/claude-sonnet-5.5", API: "openai", BaseURL: "https://litellm.example.com/v1",
			Headers: map[string]v1.HeaderValue{"x-litellm-api-key": {SecretKeyRef: &v1.SecretKeyRef{Name: "litellm", Key: "apiKey"}}},
			Limits:  &v1.ModelLimitsSpec{Context: 200000, Output: 32000},
		},
		McpServers: []v1.StandaloneMcpServerSpec{{
			Name: "serper", URL: "https://mcp.serper.dev/mcp",
			Headers: map[string]v1.HeaderValue{"X-API-KEY": {SecretKeyRef: &v1.SecretKeyRef{Name: "serper", Key: "apiKey"}}},
		}},
	}
	return p, a
}

func TestRender_StandaloneProducesV3CapabilityAndEnvBindings(t *testing.T) {
	p, a := standaloneFixture()
	cfg, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	doc := string(raw)
	for _, want := range []string{`"schemaVersion":"workspace-v3"`, `"server":"standalone"`,
		`"x-litellm-api-key":{"env":"ACH_AGENT_MODEL_H0"}`, `"X-API-KEY":{"env":"ACH_AGENT_MCP_SERPER_H0"}`,
		`"limits":{"context":200000,"output":32000}`, `"type":"openai"`,
		`"filter":{"exclude":{"tools":[],"mcpServers":[],"skills":[]}}`} {
		if !strings.Contains(doc, want) {
			t.Errorf("config lacks %s\n%s", want, doc)
		}
	}
	if strings.Contains(doc, `"ach":`) {
		t.Errorf("ach block leaked into a standalone config: %s", doc)
	}
	if cfg.SchemaVersion != "workspace-v3" {
		t.Errorf("schemaVersion = %q", cfg.SchemaVersion)
	}
	want := map[string]string{"ACH_AGENT_MODEL_H0": "litellm", "ACH_AGENT_MCP_SERPER_H0": "serper"}
	got := map[string]string{}
	for _, b := range env {
		if b.SecretKeyRef == nil {
			t.Fatalf("binding %q is not a secretKeyRef: %#v", b.Name, b)
		}
		got[b.Name] = b.SecretKeyRef.Name
	}
	if len(got) != len(want) || got["ACH_AGENT_MODEL_H0"] != "litellm" || got["ACH_AGENT_MCP_SERPER_H0"] != "serper" {
		t.Fatalf("env bindings = %#v", env)
	}
}

func TestRender_StandaloneLiteralHeaderBecomesPlainEnvBinding(t *testing.T) {
	p, a := standaloneFixture()
	a.Spec.Standalone.Model.Headers = map[string]v1.HeaderValue{"Accept-Language": {Value: ptr("en")}}
	_, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(env) < 1 || env[0].Name != "ACH_AGENT_MODEL_H0" || env[0].Value == nil || *env[0].Value != "en" || env[0].SecretKeyRef != nil {
		t.Fatalf("env = %#v", env)
	}
}

func TestRender_AchProducesKeyAliasAndModelApi(t *testing.T) {
	p, a := minimalFixture()
	a.Spec.CapabilityServer = "ach"
	a.Spec.Standalone = nil
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://api.example", Environment: "prod",
		KeySecretRef: v1.SecretKeyRef{Name: "ek", Key: "token"}, Model: v1.AchModelSpec{Name: "ackstorm.smart", API: "anthropic"}}
	cfg, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	for _, want := range []string{`"server":"ach"`, `"key":{"env":"ACH_SECRET_IDENTITY"}`, `"type":"anthropic"`, `"environment":"prod"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("config lacks %s: %s", want, raw)
		}
	}
	if strings.Contains(string(raw), `"standalone"`) {
		t.Errorf("standalone block leaked: %s", raw)
	}
	if len(env) != 1 || env[0].Name != "ACH_SECRET_IDENTITY" || env[0].SecretKeyRef.Name != "ek" || env[0].SecretKeyRef.Key != "token" {
		t.Fatalf("env = %#v", env)
	}
}

func TestRender_AchModelApiDefaultsToOpenai(t *testing.T) {
	p, a := minimalFixture()
	a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://api.example", KeySecretRef: v1.SecretKeyRef{Name: "ek", Key: "t"}, Model: v1.AchModelSpec{Name: "m"}}
	cfg, _, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Capability.Ach.Model.Type != "openai" {
		t.Fatalf("model type = %q", cfg.Capability.Ach.Model.Type)
	}
}

func TestRender_McpEnvPrefixCollisionFails(t *testing.T) {
	p, a := standaloneFixture()
	a.Spec.Standalone.McpServers = append(a.Spec.Standalone.McpServers,
		v1.StandaloneMcpServerSpec{Name: "a-b", URL: "https://x"}, v1.StandaloneMcpServerSpec{Name: "a_b", URL: "https://y"})
	if _, _, err := Render(p, a); err == nil || !strings.Contains(err.Error(), "env prefix") {
		t.Fatalf("err = %v", err)
	}
}

func TestRender_StandaloneMemoryRules(t *testing.T) {
	for name, tc := range map[string]struct {
		mem     v1.AchMemorySpec
		wantErr string
	}{
		"ach auth needs the ach server": {v1.AchMemorySpec{Endpoint: "https://m.example", Auth: &v1.AchMemoryAuthSpec{Type: "ach"}}, "needs capabilityServer ach"},
		"mcpServerId must exist":        {v1.AchMemorySpec{McpServerID: "nope"}, "is not a standalone.mcpServers entry"},
		"mcpServerId names an entry":    {v1.AchMemorySpec{McpServerID: "serper"}, ""},
	} {
		t.Run(name, func(t *testing.T) {
			p, a := standaloneFixture()
			m := tc.mem
			a.Spec.Memory = &v1.MemorySpec{Type: "ach-memory", AchMemory: &m}
			_, _, err := Render(p, a)
			if tc.wantErr == "" && err != nil {
				t.Fatal(err)
			}
			if tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("err = %v, want %q", err, tc.wantErr)
			}
		})
	}
}

func TestReferencedSecrets_IncludesHeadersAndAchKey(t *testing.T) {
	p, a := standaloneFixture()
	got := ReferencedSecrets(p, a)
	if len(got["litellm"]) != 1 || got["litellm"][0] != "apiKey" || len(got["serper"]) != 1 {
		t.Fatalf("standalone secrets = %v", got)
	}
	a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "x", KeySecretRef: v1.SecretKeyRef{Name: "ek", Key: "token"}, Model: v1.AchModelSpec{Name: "m"}}
	got = ReferencedSecrets(p, a)
	if len(got) != 1 || got["ek"][0] != "token" {
		t.Fatalf("ach secrets = %v", got)
	}
}

func TestControlServiceAccountName(t *testing.T) {
	p, _ := minimalFixture()
	if got, want := ControlServiceAccountName(fixtureAgentUID, &p), "ach-agent-control-"+fixtureAgentUID; got != want {
		t.Fatalf("unset = %q, want %q", got, want)
	}
	p.Spec.Control.ServiceAccountName = "shared-control"
	if got := ControlServiceAccountName(fixtureAgentUID, &p); got != "shared-control" {
		t.Fatalf("set = %q", got)
	}
}
