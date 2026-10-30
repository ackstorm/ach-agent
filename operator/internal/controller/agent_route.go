// SPDX-License-Identifier: MIT

package controller

import (
	"context"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// needsRoute reports whether the Agent asks for an HTTPRoute (CEL guarantees service too).
func needsRoute(a *achv1alpha1.Agent) bool {
	return a.Spec.Expose != nil && a.Spec.Expose.Route != nil
}

// buildHTTPRoute routes route.hostname + route.path to the Agent's -http Service, stripping a
// non-root path so the control pod sees /channels/... at its root. The parentRef is
// route.parentRef, else def.
func buildHTTPRoute(a *achv1alpha1.Agent, def achv1alpha1.ParentRef) *gatewayv1.HTTPRoute {
	route := a.Spec.Expose.Route
	parent := def
	if route.ParentRef != nil {
		parent = *route.ParentRef
	}
	ref := gatewayv1.ParentReference{Name: gatewayv1.ObjectName(parent.Name)}
	if parent.Namespace != "" {
		ref.Namespace = ptr.To(gatewayv1.Namespace(parent.Namespace))
	}
	if parent.SectionName != "" {
		ref.SectionName = ptr.To(gatewayv1.SectionName(parent.SectionName))
	}
	path := route.Path
	if path == "" {
		path = "/"
	}
	var filters []gatewayv1.HTTPRouteFilter
	if path != "/" {
		filters = []gatewayv1.HTTPRouteFilter{{
			Type: gatewayv1.HTTPRouteFilterURLRewrite,
			URLRewrite: &gatewayv1.HTTPURLRewriteFilter{Path: &gatewayv1.HTTPPathModifier{
				Type: gatewayv1.PrefixMatchHTTPPathModifier, ReplacePrefixMatch: ptr.To("/"),
			}},
		}}
	}
	return &gatewayv1.HTTPRoute{
		ObjectMeta: metav1.ObjectMeta{Name: render.ControlName(a.Name), Namespace: a.Namespace, Labels: agentLabels(a)},
		Spec: gatewayv1.HTTPRouteSpec{
			CommonRouteSpec: gatewayv1.CommonRouteSpec{ParentRefs: []gatewayv1.ParentReference{ref}},
			Hostnames:       []gatewayv1.Hostname{gatewayv1.Hostname(route.Hostname)},
			Rules: []gatewayv1.HTTPRouteRule{{
				Matches: []gatewayv1.HTTPRouteMatch{{Path: &gatewayv1.HTTPPathMatch{
					Type: ptr.To(gatewayv1.PathMatchPathPrefix), Value: ptr.To(path),
				}}},
				Filters: filters,
				BackendRefs: []gatewayv1.HTTPBackendRef{{BackendRef: gatewayv1.BackendRef{
					BackendObjectReference: gatewayv1.BackendObjectReference{
						Name: gatewayv1.ObjectName(httpServiceName(a.Name)),
						Port: ptr.To(gatewayv1.PortNumber(controlProbePort)),
					},
				}}},
			}},
		},
	}
}

// reconcileRoute applies or prunes the HTTPRoute. It returns a non-nil result when the
// reconcile must end here (status already written).
func (r *AgentReconciler) reconcileRoute(ctx context.Context, a *achv1alpha1.Agent, conds []metav1.Condition) (*ctrl.Result, error) {
	if !needsRoute(a) {
		if !r.HTTPRouteAvailable {
			return nil, nil
		}
		return nil, r.prune(ctx, a, &gatewayv1.HTTPRoute{ObjectMeta: metav1.ObjectMeta{Name: render.ControlName(a.Name), Namespace: a.Namespace}})
	}
	if !r.HTTPRouteAvailable {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "RouteUnsupported",
			"expose.route needs the Gateway API HTTPRoute CRD (gateway.networking.k8s.io/v1), which this cluster does not serve", a.Generation)
		res, err := r.finish(ctx, a, conds)
		return &res, err
	}
	if r.RouteParent.Name == "" && (a.Spec.Expose.Route.ParentRef == nil) {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "RouteUnsupported",
			"expose.route has no parentRef and the operator has no default Gateway (ACH_AGENT_ROUTE_PARENT_NAME)", a.Generation)
		res, err := r.finish(ctx, a, conds)
		return &res, err
	}
	if err := r.apply(ctx, a, buildHTTPRoute(a, r.RouteParent)); err != nil {
		res, ferr := r.applyFail(ctx, a, conds, "HTTPRoute", err)
		return &res, ferr
	}
	return nil, nil
}
