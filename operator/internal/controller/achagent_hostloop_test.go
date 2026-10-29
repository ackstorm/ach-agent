// SPDX-License-Identifier: MIT

package controller

import (
	"testing"

	"github.com/ackstorm/ach-agent/operator/internal/host"
)

// The host loop reads the key Secret the reconciler creates; they live in different packages
// (the loop's package must not import this one), so pin the shared convention here.
func TestWorkspaceKeySecretConventionMatchesTheHostLoop(t *testing.T) {
	if got, want := host.KeySecretName("uid-1"), workspaceKeySecretName("uid-1"); got != want {
		t.Errorf("KeySecretName = %q, reconciler creates %q", got, want)
	}
	if host.KeySecretDataKey != workspaceKeyDataKey {
		t.Errorf("KeySecretDataKey = %q, reconciler stores under %q", host.KeySecretDataKey, workspaceKeyDataKey)
	}
}
