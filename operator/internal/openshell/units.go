// SPDX-License-Identifier: MIT

package openshell

import (
	"regexp"
	"strings"
)

const (
	// AgentUIDLabel and NameLabel mark a sandbox and its Service with the owning Agent and
	// the runtime Workspace name.
	AgentUIDLabel = "runtime.ach-agent.ackstorm.ai/agent-uid"
	NameLabel     = "runtime.ach-agent.ackstorm.ai/workspace-name"
)

var plainVersion = regexp.MustCompile(`^v?\d+\.\d+\.\d+$`)

// imageRelease is the tag of a plain-version image ("0.1.23" or "v0.1.23" -> "0.1.23"); ""
// for a digest pin, an untagged ref, or a non-version tag such as "dev".
func imageRelease(image string) string {
	if strings.Contains(image, "@") {
		return ""
	}
	i := strings.LastIndex(image, ":")
	if i < 0 || strings.Contains(image[i:], "/") {
		return ""
	}
	if tag := image[i+1:]; plainVersion.MatchString(tag) {
		return strings.TrimPrefix(tag, "v")
	}
	return ""
}
