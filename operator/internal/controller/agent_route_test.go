// SPDX-License-Identifier: MIT

package controller

import (
	"testing"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

func routeAgent(name string, route *achv1alpha1.RouteSpec) *achv1alpha1.Agent {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = name, "ns"
	a.Spec.Expose = &achv1alpha1.ExposeSpec{Service: true, Route: route}
	return a
}

func TestBuildHTTPRoute_UsesHostnamePathAndDefaultParent(t *testing.T) {
	a := routeAgent("researcher", &achv1alpha1.RouteSpec{Hostname: "r.example.com", Path: "/api"})
	route := buildHTTPRoute(a, achv1alpha1.ParentRef{Name: "public", Namespace: "gateway", SectionName: "https"})
	if len(route.Spec.Hostnames) != 1 || string(route.Spec.Hostnames[0]) != "r.example.com" {
		t.Fatalf("hostnames = %v", route.Spec.Hostnames)
	}
	parent := route.Spec.ParentRefs[0]
	if string(parent.Name) != "public" || string(*parent.Namespace) != "gateway" || string(*parent.SectionName) != "https" {
		t.Fatalf("parentRef = %#v", parent)
	}
	rule := route.Spec.Rules[0]
	if *rule.Matches[0].Path.Value != "/api" {
		t.Fatalf("path = %v", *rule.Matches[0].Path.Value)
	}
	backend := rule.BackendRefs[0]
	if string(backend.Name) != render.ControlName("researcher")+"-http" || *backend.Port != 8080 {
		t.Fatalf("backend = %#v", backend)
	}
	if route.Name != render.ControlName("researcher") || route.Namespace != "ns" {
		t.Fatalf("route name = %s/%s", route.Namespace, route.Name)
	}
}

func TestBuildHTTPRoute_AgentParentRefWinsAndPathDefaultsToRoot(t *testing.T) {
	a := routeAgent("a", &achv1alpha1.RouteSpec{Hostname: "a.example.com", ParentRef: &achv1alpha1.ParentRef{Name: "internal"}})
	route := buildHTTPRoute(a, achv1alpha1.ParentRef{Name: "public", Namespace: "gateway"})
	parent := route.Spec.ParentRefs[0]
	if string(parent.Name) != "internal" || parent.Namespace != nil {
		t.Fatalf("parentRef = %#v, want the Agent's own", parent)
	}
	if *route.Spec.Rules[0].Matches[0].Path.Value != "/" {
		t.Fatalf("path = %q, want /", *route.Spec.Rules[0].Matches[0].Path.Value)
	}
}
