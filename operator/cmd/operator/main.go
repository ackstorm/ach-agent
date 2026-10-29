// SPDX-License-Identifier: MIT

// Command operator runs the ach-agent controller and the per-Agent sandbox host loops.
package main

import (
	"flag"
	"os"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

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
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, achv1alpha1.AddToScheme} {
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
	if err := (&controller.ACHAgentReconciler{
		Client: mgr.GetClient(), APIReader: mgr.GetAPIReader(), Scheme: mgr.GetScheme(),
		RuntimeStorageOptions: storage,
		Sandboxes:             sandboxes,
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "register Agent controller")
		os.Exit(1)
	}
	if err := mgr.Add(&host.Runner{
		Client: mgr.GetClient(), Reader: mgr.GetAPIReader(), Scheme: mgr.GetScheme(),
		Log:        ctrl.Log.WithName("sandbox-host"),
		NewBackend: sandboxes.ForAgent,
	}); err != nil {
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
