// SPDX-License-Identifier: MIT

package render

import (
	"reflect"
	"testing"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func TestHeaderEnv_SortsCaseInsensitivelyAndBindsSecretsAndLiterals(t *testing.T) {
	aliases, bindings := HeaderEnv("ACH_AGENT_MODEL", map[string]v1.HeaderValue{
		"x-litellm-api-key": {SecretKeyRef: &v1.SecretKeyRef{Name: "litellm", Key: "apiKey"}},
		"Accept-Language":   {Value: ptr("en")},
	})
	wantAliases := map[string]WSSecretAlias{
		"Accept-Language":   {Env: "ACH_AGENT_MODEL_H0"},
		"x-litellm-api-key": {Env: "ACH_AGENT_MODEL_H1"},
	}
	if !reflect.DeepEqual(aliases, wantAliases) {
		t.Fatalf("aliases = %#v", aliases)
	}
	if bindings[0].Name != "ACH_AGENT_MODEL_H0" || *bindings[0].Value != "en" {
		t.Fatalf("binding 0 = %#v", bindings[0])
	}
	if bindings[1].Name != "ACH_AGENT_MODEL_H1" || bindings[1].SecretKeyRef.Name != "litellm" {
		t.Fatalf("binding 1 = %#v", bindings[1])
	}
}

func TestMcpEnvPrefix_UppercasesAndReplacesDashes(t *testing.T) {
	if got := McpEnvPrefix("web-search"); got != "ACH_AGENT_MCP_WEB_SEARCH" {
		t.Fatalf("got %q", got)
	}
}

func TestCheckHeaderNames_RejectsCaseVariants(t *testing.T) {
	err := checkHeaderNames("x", map[string]v1.HeaderValue{"X-Key": {Value: ptr("a")}, "x-key": {Value: ptr("b")}})
	if err == nil {
		t.Fatal("headers differing only by case must be rejected")
	}
}

func TestCheckHeaderNames_RejectsReservedNames(t *testing.T) {
	for _, name := range []string{"Host", "content-length", "Transfer-Encoding", "CONNECTION", "X-Ach-Key", "x-ach-anything",
		"traceparent", "TraceState", "x-litellm-session-id", "X-LiteLLM-Trace-Id", "langfuse_user_id", "Langfuse_Tag"} {
		if err := checkHeaderNames("x", map[string]v1.HeaderValue{name: {Value: ptr("a")}}); err == nil {
			t.Errorf("header %q must be rejected", name)
		}
	}
	if err := checkHeaderNames("x", map[string]v1.HeaderValue{"x-litellm-api-key": {Value: ptr("a")}, "Authorization": {Value: ptr("b")}}); err != nil {
		t.Errorf("ordinary headers rejected: %v", err)
	}
}

func TestCheckHeaderNames_RejectsInvalidTokens(t *testing.T) {
	for _, name := range []string{"bad name", "bad:name", "bad/name", "näme", "a,b"} {
		if err := checkHeaderNames("x", map[string]v1.HeaderValue{name: {Value: ptr("a")}}); err == nil {
			t.Errorf("header %q must be rejected", name)
		}
	}
}
