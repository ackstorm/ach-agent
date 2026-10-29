// SPDX-License-Identifier: MIT

package openshell

import (
	"errors"
	"fmt"
	"path"
	"strings"

	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
	sbv1 "github.com/NVIDIA/OpenShell/sdk/go/proto/sandboxv1"
	"google.golang.org/protobuf/encoding/protojson"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/third_party/openshellconv"
)

var (
	// ErrPolicyRejected marks a sandbox policy the operator or the gateway refuses.
	ErrPolicyRejected = errors.New("sandbox policy rejected")
	// ErrResourcesUnsupported marks sandbox resources the gateway refuses.
	ErrResourcesUnsupported = errors.New("sandbox resources unsupported")
)

// reservedPrefix starts the names of the rules the operator owns.
const reservedPrefix = "ach-agent-"

// runtimeBinaries are the only binaries allowed to reach the facades; they and their
// directories stay read-only so a sandboxed process cannot replace a trusted binary.
var runtimeBinaries = []string{
	"/usr/local/bin/python3.13",
	"/usr/local/bin/node",
	// node_modules/.bin/opencode2 symlinks to this native ELF binary, not a node script: the
	// engine process that calls the model facade is this executable.
	"/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe",
}

var (
	runtimeReadOnly  = []string{"/usr", "/lib", "/etc", "/bin", "/sbin", "/opt", "/work"}
	runtimeReadWrite = []string{"/tmp", "/workspace", "/var/lib/ach-runtime", "/home/ach"}
	// /work holds the runtime's code (PYTHONPATH), /usr/local/lib the Python stdlib and site-packages.
	protectedDirs = []string{"/usr/local/bin", "/usr/local/lib", "/opt/opencode", "/work"}
)

// decode reads a policy document strictly: protojson rejects unknown fields.
func decode(doc *apiextensionsv1.JSON) (*ostypes.SandboxPolicy, error) {
	if doc == nil || len(doc.Raw) == 0 {
		return &ostypes.SandboxPolicy{}, nil
	}
	p := &sbv1.SandboxPolicy{}
	if err := (protojson.UnmarshalOptions{}).Unmarshal(doc.Raw, p); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrPolicyRejected, err)
	}
	return openshellconv.SandboxPolicyFromProto(p), nil
}

func appendUnique(dst []string, add ...string) []string {
	for _, a := range add {
		found := false
		for _, d := range dst {
			found = found || d == a
		}
		if !found {
			dst = append(dst, a)
		}
	}
	return dst
}

// BuildPolicy merges the profile's policy with the Agent's (network rules by name, Agent
// wins; filesystem lists concatenated; scalars Agent over profile), rejects what the operator
// reserves, then adds the reserved rules: the facade egress rule, the runtime paths and the
// landlock default.
func BuildPolicy(profile, agent *apiextensionsv1.JSON, facadeHost string, facadePort uint32) (*ostypes.SandboxPolicy, error) {
	merged, err := decode(profile)
	if err != nil {
		return nil, err
	}
	top, err := decode(agent)
	if err != nil {
		return nil, err
	}
	if merged.NetworkPolicies == nil {
		merged.NetworkPolicies = map[string]ostypes.NetworkPolicyRule{}
	}
	for name, rule := range top.NetworkPolicies {
		merged.NetworkPolicies[name] = rule
	}
	for name, rule := range merged.NetworkPolicies {
		if isReserved(name) || isReserved(rule.Name) {
			return nil, fmt.Errorf("%w: rule %q (name %q) uses the reserved prefix %s", ErrPolicyRejected, name, rule.Name, reservedPrefix)
		}
		if rule.Name != "" && rule.Name != name {
			return nil, fmt.Errorf("%w: rule name %q differs from its key %q", ErrPolicyRejected, rule.Name, name)
		}
		for _, ep := range rule.Endpoints {
			if reachesFacade(ep, facadeHost, facadePort) {
				return nil, fmt.Errorf("%w: rule %q could match the facade endpoint %s:%d", ErrPolicyRejected, name, facadeHost, facadePort)
			}
		}
	}
	if merged.Filesystem == nil {
		merged.Filesystem = &ostypes.FilesystemPolicy{}
	}
	if top.Filesystem != nil {
		merged.Filesystem.ReadOnly = appendUnique(merged.Filesystem.ReadOnly, top.Filesystem.ReadOnly...)
		merged.Filesystem.ReadWrite = appendUnique(merged.Filesystem.ReadWrite, top.Filesystem.ReadWrite...)
	}
	if top.Landlock != nil {
		merged.Landlock = top.Landlock
	}
	if top.Process != nil {
		merged.Process = top.Process
	}
	if len(top.NetworkMiddlewares) > 0 {
		if merged.NetworkMiddlewares == nil {
			merged.NetworkMiddlewares = map[string]ostypes.NetworkMiddlewareConfig{}
		}
		for name, m := range top.NetworkMiddlewares {
			merged.NetworkMiddlewares[name] = m
		}
	}
	for name, m := range merged.NetworkMiddlewares {
		if isReserved(name) || isReserved(m.Name) {
			return nil, fmt.Errorf("%w: middleware %q (name %q) uses the reserved prefix %s", ErrPolicyRejected, name, m.Name, reservedPrefix)
		}
		if m.Endpoints != nil {
			for _, glob := range m.Endpoints.Include {
				if hostMatches(glob, facadeHost) {
					return nil, fmt.Errorf("%w: middleware %q selects the facade host %s", ErrPolicyRejected, name, facadeHost)
				}
			}
		}
	}
	for _, p := range merged.Filesystem.ReadOnly {
		if !path.IsAbs(p) {
			return nil, fmt.Errorf("%w: read_only %q must be an absolute path", ErrPolicyRejected, p)
		}
	}
	for _, rw := range merged.Filesystem.ReadWrite {
		if err := checkWritable(rw); err != nil {
			return nil, err
		}
	}

	// Reserved rules last, so nothing above can override them. The workdir is not added:
	// the writable paths are listed explicitly, and the image WORKDIR (/work) holds code.
	merged.Version = 1
	merged.Filesystem.IncludeWorkdir = false
	merged.Filesystem.ReadOnly = appendUnique(merged.Filesystem.ReadOnly, runtimeReadOnly...)
	merged.Filesystem.ReadWrite = appendUnique(merged.Filesystem.ReadWrite, runtimeReadWrite...)
	if merged.Landlock == nil || merged.Landlock.Compatibility == "" {
		merged.Landlock = &ostypes.LandlockPolicy{Compatibility: "hard_requirement"}
	}
	bins := make([]ostypes.PolicyNetworkBinary, 0, len(runtimeBinaries))
	for _, b := range runtimeBinaries {
		bins = append(bins, ostypes.PolicyNetworkBinary{Path: b})
	}
	merged.NetworkPolicies[reservedPrefix+"facades"] = ostypes.NetworkPolicyRule{
		Name:      reservedPrefix + "facades",
		Endpoints: []ostypes.PolicyNetworkEndpoint{{Host: facadeHost, Port: facadePort}},
		Binaries:  bins,
	}
	return merged, nil
}

func isReserved(name string) bool {
	return strings.HasPrefix(strings.ToLower(name), reservedPrefix)
}

// hostMatches reports whether a host glob (or exact host) selects the facade host.
// Case-insensitive, trailing dot ignored; an unparsable glob counts as a match.
func hostMatches(glob, facadeHost string) bool {
	glob = strings.TrimSuffix(strings.ToLower(glob), ".")
	host := strings.TrimSuffix(strings.ToLower(facadeHost), ".")
	if glob == "" {
		return true
	}
	ok, err := path.Match(glob, host)
	return ok || err != nil
}

// reachesFacade reports whether an endpoint of a non-reserved rule could carry traffic to the
// facade: a matching host, or any allowed_ips (the control pod's IP is not known here), on a
// port that includes the facade port (no port listed means any).
func reachesFacade(ep ostypes.PolicyNetworkEndpoint, facadeHost string, facadePort uint32) bool {
	ports := append([]uint32{ep.Port}, ep.Ports...)
	portMatch := false
	for _, p := range ports {
		portMatch = portMatch || p == 0 || p == facadePort
	}
	return portMatch && (hostMatches(ep.Host, facadeHost) || len(ep.AllowedIPs) > 0)
}

// checkWritable rejects a read-write path equal to, inside or above a protected directory:
// the runtime binaries must stay read-only.
func checkWritable(p string) error {
	if !path.IsAbs(p) {
		return fmt.Errorf("%w: read_write %q must be an absolute path", ErrPolicyRejected, p)
	}
	clean := path.Clean(p)
	if clean == "/" {
		return fmt.Errorf("%w: read_write %q would make runtime binaries writable", ErrPolicyRejected, p)
	}
	for _, dir := range protectedDirs {
		if clean == dir || strings.HasPrefix(clean, dir+"/") || strings.HasPrefix(dir, clean+"/") {
			return fmt.Errorf("%w: read_write %q would make runtime binaries under %s writable", ErrPolicyRejected, p, dir)
		}
	}
	return nil
}

// Resources maps the profile's sandbox resources onto the template's resource map.
func Resources(r *achv1alpha1.SandboxResources) (map[string]any, error) {
	if r == nil {
		return nil, nil
	}
	out := map[string]any{}
	if r.CPU != "" {
		out["cpu"] = r.CPU
	}
	if r.Memory != "" {
		out["memory"] = r.Memory
	}
	if r.EphemeralStorage != "" {
		out["ephemeral-storage"] = r.EphemeralStorage
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}
