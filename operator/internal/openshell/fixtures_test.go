// SPDX-License-Identifier: MIT

package openshell

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
)

const testUID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := achv1alpha1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

func unitFixture() (*achv1alpha1.Agent, *achv1alpha1.AgentProfile, host.Workspace) {
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "gitlab-reviewer", Namespace: "ach", UID: testUID}}
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Sandbox.OpenShell = achv1alpha1.OpenShellSpec{Image: "ghcr.io/ackstorm/ach-runtime-execution:dev"}
	w := host.Workspace{
		Name: "agent-gitlab-reviewer-1a2b3c4d5e6f7a8b9c0d", WorkspaceRef: strings.Repeat("ab", 32), Desired: "running",
		Launch: host.Launch{
			RuntimeRelease: "0.1.22", Port: 8080,
			Command: []string{"python", "-m", "ach_runtime", "execution"},
			Env: map[string]string{
				"ACH_SANDBOX_VERIFY_KEY": "pub", "ACH_WORKSPACE_NAME": "agent-gitlab-reviewer-1a2b3c4d5e6f7a8b9c0d",
				"ACH_WORKSPACE_REF": strings.Repeat("ab", 32), "ACH_BOOTSTRAP_JSON": `{"bootstrapVersion":"workspace-v3"}`,
			},
		},
	}
	return a, p, w
}
