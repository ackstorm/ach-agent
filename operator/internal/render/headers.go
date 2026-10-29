// SPDX-License-Identifier: MIT

package render

import (
	"fmt"
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

// HeaderEnv names one env var per header (<prefix>_H<i>, i in case-insensitive name order)
// and returns the config aliases plus the pod bindings.
func HeaderEnv(prefix string, headers map[string]v1.HeaderValue) (map[string]WSSecretAlias, []EnvBinding) {
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool { return strings.ToLower(names[i]) < strings.ToLower(names[j]) })
	aliases := make(map[string]WSSecretAlias, len(names))
	bindings := make([]EnvBinding, 0, len(names))
	for i, name := range names {
		env := fmt.Sprintf("%s_H%d", prefix, i)
		aliases[name] = WSSecretAlias{Env: env}
		h := headers[name]
		bindings = append(bindings, EnvBinding{Name: env, SecretKeyRef: h.SecretKeyRef, Value: h.Value})
	}
	return aliases, bindings
}

// McpEnvPrefix maps an MCP server name to its env prefix.
func McpEnvPrefix(server string) string {
	return "ACH_AGENT_MCP_" + strings.ToUpper(strings.ReplaceAll(server, "-", "_"))
}

// reservedHeaders are never configurable: the proxy and the HTTP framing own them.
var reservedHeaders = map[string]struct{}{"host": {}, "content-length": {}, "transfer-encoding": {}, "connection": {}}

// checkHeaderNames rejects reserved names and two names that differ only by case: header
// names are case-insensitive and unique per endpoint.
func checkHeaderNames(where string, headers map[string]v1.HeaderValue) error {
	seen := make(map[string]string, len(headers))
	for name := range headers {
		lower := strings.ToLower(name)
		if _, ok := reservedHeaders[lower]; ok || strings.HasPrefix(lower, "x-ach-") {
			return fmt.Errorf("%s: header %q is reserved", where, name)
		}
		if prev, ok := seen[lower]; ok {
			return fmt.Errorf("%s: headers %q and %q are the same header", where, prev, name)
		}
		seen[lower] = name
	}
	return nil
}
