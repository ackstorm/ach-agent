// SPDX-License-Identifier: MIT

package render

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// EnvBinding is one env var the control pod needs: from a Secret key or a literal.
type EnvBinding struct {
	Name         string
	SecretKeyRef *v1.SecretKeyRef
	Value        *string
}

// HeaderEnv renders every header for the wire: a Secret key becomes the alias
// {"env": <prefix>_H<i>} (i in case-insensitive name order, over all headers) with a pod
// binding; a literal becomes {"value": ...} and gets no binding, so the runtime never treats
// it as a secret.
func HeaderEnv(prefix string, headers map[string]v1.HeaderValue) (map[string]WSHeader, []EnvBinding) {
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool { return strings.ToLower(names[i]) < strings.ToLower(names[j]) })
	aliases := make(map[string]WSHeader, len(names))
	bindings := make([]EnvBinding, 0, len(names))
	for i, name := range names {
		h := headers[name]
		if h.SecretKeyRef == nil {
			aliases[name] = WSHeader{Value: h.Value}
			continue
		}
		env := fmt.Sprintf("%s_H%d", prefix, i)
		aliases[name] = WSHeader{Env: env}
		bindings = append(bindings, EnvBinding{Name: env, SecretKeyRef: h.SecretKeyRef})
	}
	return aliases, bindings
}

// McpEnvPrefix maps an MCP server name to its env prefix.
func McpEnvPrefix(server string) string {
	return "ACH_AGENT_MCP_" + strings.ToUpper(strings.ReplaceAll(server, "-", "_"))
}

// The header rules mirror the runtime's config validation (ach_runtime/config/schema.py): the
// proxy, the HTTP framing and the tracing layer own the reserved names and prefixes.
var (
	reservedHeaders = map[string]struct{}{
		"host": {}, "content-length": {}, "transfer-encoding": {}, "connection": {},
		"traceparent": {}, "tracestate": {}, "x-litellm-session-id": {}, "x-litellm-trace-id": {},
	}
	reservedHeaderPrefixes = []string{"x-ach-", "langfuse_"}
	headerNameRE           = regexp.MustCompile("^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
)

// checkHeaderNames rejects reserved names and two names that differ only by case: header
// names are case-insensitive and unique per endpoint.
func checkHeaderNames(where string, headers map[string]v1.HeaderValue) error {
	seen := make(map[string]string, len(headers))
	for name := range headers {
		lower := strings.ToLower(name)
		if !headerNameRE.MatchString(name) {
			return fmt.Errorf("%s: invalid header name %q", where, name)
		}
		if _, ok := reservedHeaders[lower]; ok || hasAnyPrefix(lower, reservedHeaderPrefixes) {
			return fmt.Errorf("%s: header %q is reserved", where, name)
		}
		if prev, ok := seen[lower]; ok {
			return fmt.Errorf("%s: headers %q and %q are the same header", where, prev, name)
		}
		seen[lower] = name
	}
	return nil
}

func hasAnyPrefix(s string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}
