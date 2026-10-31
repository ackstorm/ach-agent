// SPDX-License-Identifier: MIT

package openshell

import (
	"context"
	"testing"

	osv1 "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1"
	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
)

// workspaceGateway knows only the workspaces in `known`; the rest answer NotFound, as the gateway does.
type workspaceGateway struct {
	osv1.SandboxInterface
	known   map[string]bool
	created []string
}

func (g *workspaceGateway) notFound(ws string) error {
	if g.known[ws] {
		return nil
	}
	return &osv1.StatusError{Code: osv1.ErrorNotFound, Message: "workspace '" + ws + "' not found"}
}

func (g *workspaceGateway) ListAll(_ context.Context, ws string, _ ...ostypes.ListOptions) ([]*ostypes.Sandbox, error) {
	return nil, g.notFound(ws)
}

func (g *workspaceGateway) Create(_ context.Context, ws, name string, _ *ostypes.SandboxSpec, _ ...ostypes.CreateOption) (*ostypes.Sandbox, error) {
	if err := g.notFound(ws); err != nil {
		return nil, err
	}
	return &ostypes.Sandbox{Name: name}, nil
}

type workspaceMaker struct{ gw *workspaceGateway }

func (m workspaceMaker) Create(_ context.Context, name string, _ map[string]string) (*osv1.Workspace, error) {
	m.gw.known[name] = true
	m.gw.created = append(m.gw.created, name)
	return &osv1.Workspace{}, nil
}

func TestClientBox_UnknownWorkspaceListsEmptyAndIsCreatedOnFirstSandbox(t *testing.T) {
	ctx := context.Background()
	gw := &workspaceGateway{known: map[string]bool{}}
	c := clientBox{SandboxInterface: gw, workspaces: workspaceMaker{gw}}

	if l, err := c.ListAll(ctx, "ach-agent"); err != nil || len(l) != 0 {
		t.Fatalf("ListAll on an unknown workspace = %v, %v; want empty, nil", l, err)
	}
	s, err := c.Create(ctx, "ach-agent", "k8s-doctor-9f0456d5", &ostypes.SandboxSpec{})
	if err != nil || s.Name != "k8s-doctor-9f0456d5" {
		t.Fatalf("Create = %v, %v; want the sandbox created after the workspace", s, err)
	}
	if len(gw.created) != 1 || gw.created[0] != "ach-agent" {
		t.Fatalf("workspaces created = %v, want [ach-agent]", gw.created)
	}
	if _, err := c.Create(ctx, "ach-agent", "other-0123abcd", &ostypes.SandboxSpec{}); err != nil || len(gw.created) != 1 {
		t.Fatalf("second Create: err %v, workspaces created %v; want no second workspace create", err, gw.created)
	}
}
