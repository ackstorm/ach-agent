// SPDX-License-Identifier: MIT

package host

import (
	"context"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// The host logic (inventory first, nothing while adopting, a stop confirmed only once it is
// confirmed, unlisted units removed) lives in Host (host.go). A backend only answers "which
// units exist and in which state", creates them and removes them.

type UnitState int

const (
	UnitAbsent      UnitState = iota // nothing of the unit exists
	UnitTerminating                  // deletion requested, something remains
	UnitStarting                     // exists, not yet registrable
	UnitStarted                      // registrable (sandbox phase Ready, workload pod running)
	UnitLost                         // the process is gone but the record remains (Ready sandbox, workload pod missing, terminating or not Running)
)

type Backend interface {
	State(ctx context.Context, name string) (UnitState, error)
	Create(ctx context.Context, a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile, w Workspace) error
	BaseURL(w Workspace) string
	// Remove requests deletion of the unit and reports whether anything of it remains, i.e.
	// whether its process may still run. Never waits, never forces.
	Remove(ctx context.Context, name string) (remaining bool, err error)
	// Owned lists the Workspace names of every unit of this agent, in any state.
	Owned(ctx context.Context) ([]string, error)
	// Live lists the started, not terminating units for the inventory.
	Live(ctx context.Context) ([]InventoryUnit, error)
}
