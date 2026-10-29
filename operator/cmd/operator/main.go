// SPDX-License-Identifier: MIT

// Command operator runs the ach-agent controller and the per-Agent sandbox host loops.
package main

import (
	"flag"
	"os"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/controller"
	"github.com/ackstorm/ach-agent/operator/internal/host"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const leaderElectionID = "ach-agent-operator.ackstorm.ai"

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// httpRouteServed reports whether the cluster serves gateway.networking.k8s.io/v1 httproutes.
func httpRouteServed(cfg *rest.Config) (bool, error) {
	dc, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		return false, err
	}
	list, err := dc.ServerResourcesForGroupVersion(gatewayv1.GroupVersion.String())
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	for _, r := range list.APIResources {
		if r.Name == "httproutes" {
			return true, nil
		}
	}
	return false, nil
}

func main() {
	var metricsAddr, probeAddr string
	var leaderElect bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "address the metrics endpoint binds to")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "address the probe endpoint binds to")
	flag.BoolVar(&leaderElect, "leader-elect", false, "enable leader election")
	zapOpts := zap.Options{Development: true}
	zapOpts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&zapOpts)))
	setupLog := ctrl.Log.WithName("setup")

	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, achv1alpha1.AddToScheme, gatewayv1.Install} {
		if err := add(scheme); err != nil {
			setupLog.Error(err, "register scheme")
			os.Exit(1)
		}
	}

	sandboxes, err := openshell.OpenShellFromEnv()
	if err != nil {
		setupLog.Error(err, "OpenShell gateway")
		os.Exit(1)
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress:  probeAddr,
		LeaderElection:          leaderElect,
		LeaderElectionID:        leaderElectionID,
		LeaderElectionNamespace: os.Getenv("ACH_AGENT_NAMESPACE"),
	})
	if err != nil {
		setupLog.Error(err, "create manager")
		os.Exit(1)
	}

	storage := render.RuntimeStorageOptions{
		Bucket:                os.Getenv("ACH_AGENT_STORAGE_S3_BUCKET"),
		Region:                os.Getenv("ACH_AGENT_STORAGE_S3_REGION"),
		EndpointURL:           os.Getenv("ACH_AGENT_STORAGE_S3_ENDPOINT_URL"),
		Prefix:                envOr("ACH_AGENT_STORAGE_S3_PREFIX", "ach"),
		CredentialsSecretName: os.Getenv("ACH_AGENT_STORAGE_S3_CREDENTIALS_SECRET"),
	}
	routeAvailable, err := httpRouteServed(mgr.GetConfig())
	if err != nil {
		setupLog.Error(err, "discover Gateway API")
		os.Exit(1)
	}
	runner := &host.Runner{
		Client: mgr.GetClient(), Reader: mgr.GetAPIReader(), Scheme: mgr.GetScheme(),
		Log:        ctrl.Log.WithName("sandbox-host"),
		NewBackend: sandboxes.ForAgent,
	}
	if err := (&controller.AgentReconciler{
		Client: mgr.GetClient(), APIReader: mgr.GetAPIReader(), Scheme: mgr.GetScheme(),
		Storage:   storage,
		Sandboxes: sandboxes,
		RouteParent: achv1alpha1.ParentRef{
			Name:        os.Getenv("ACH_AGENT_ROUTE_PARENT_NAME"),
			Namespace:   os.Getenv("ACH_AGENT_ROUTE_PARENT_NAMESPACE"),
			SectionName: os.Getenv("ACH_AGENT_ROUTE_PARENT_SECTION"),
		},
		HTTPRouteAvailable: routeAvailable,
		Host:               runner,
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "register Agent controller")
		os.Exit(1)
	}
	if err := mgr.Add(runner); err != nil {
		setupLog.Error(err, "register host runner")
		os.Exit(1)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "healthz")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "readyz")
		os.Exit(1)
	}
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "run manager")
		os.Exit(1)
	}
}
