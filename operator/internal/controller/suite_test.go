// SPDX-License-Identifier: MIT

// Package-level envtest bootstrap for the Agent reconciler suite. KUBEBUILDER_ASSETS comes
// from the Makefile (make test-envtest); CRDs are the generated ones under config/crd.

package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// WatchNamespace is the namespace the envtest fixtures live in.
const WatchNamespace = "ach-system"

// testRuntimeStorageOptions is the fake global Storage backend every reconcile test in this
// suite runs against: a configured bucket, so an agent that opts into persistence/artifacts
// sees WorkloadApplied reach True. ValidateRuntimeStorage checks configuration only.
func testRuntimeStorageOptions() render.RuntimeStorageOptions {
	return render.RuntimeStorageOptions{Bucket: "test-bucket", Prefix: "ach"}
}

var (
	testEnv   *envtest.Environment
	cfg       *rest.Config
	k8sClient client.Client
	mgrCtx    context.Context
	mgrCancel context.CancelFunc
)

func TestMain(m *testing.M) {
	ctrl.SetLogger(zap.New(zap.UseDevMode(true), zap.WriteTo(os.Stderr)))
	if code := setupAndRun(m); code != 0 {
		os.Exit(code)
	}
}

// setupAndRun owns the envtest lifecycle so deferred cleanup runs before the process exits.
func setupAndRun(m *testing.M) int {
	_, thisFile, _, _ := runtime.Caller(0)
	crdDir := filepath.Join(filepath.Dir(thisFile), "..", "..", "config", "crd")
	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{crdDir},
		ErrorIfCRDPathMissing: true,
	}

	var err error
	cfg, err = testEnv.Start()
	if err != nil {
		fmt.Fprintf(os.Stderr, "envtest start failed: %v\n", err)
		return 1
	}
	defer func() {
		if err := testEnv.Stop(); err != nil {
			fmt.Fprintf(os.Stderr, "envtest stop: %v\n", err)
		}
	}()

	scheme := k8sruntime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(achv1alpha1.AddToScheme(scheme))

	k8sClient, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		fmt.Fprintf(os.Stderr, "k8s client: %v\n", err)
		return 1
	}
	ctx := context.Background()
	if err := k8sClient.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: WatchNamespace}}); err != nil && !apierrors.IsAlreadyExists(err) {
		fmt.Fprintf(os.Stderr, "ensure %s: %v\n", WatchNamespace, err)
		return 1
	}

	// The shared manager watches WatchNamespace only: tests in other namespaces drive their own
	// reconciler through Reconcile() without racing it.
	mgr, err := manager.New(cfg, manager.Options{
		Scheme:                 scheme,
		Cache:                  cache.Options{DefaultNamespaces: map[string]cache.Config{WatchNamespace: {}}},
		HealthProbeBindAddress: "0",
		LeaderElection:         false,
		Metrics:                metricsserver.Options{BindAddress: "0"},
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "manager.New: %v\n", err)
		return 1
	}
	if err := (&AgentReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Scheme:    mgr.GetScheme(),
		Storage:   testRuntimeStorageOptions(),
	}).SetupWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "SetupWithManager(Agent): %v\n", err)
		return 1
	}

	mgrCtx, mgrCancel = context.WithCancel(ctx)
	defer mgrCancel()
	mgrDone := make(chan error, 1)
	go func() { mgrDone <- mgr.Start(mgrCtx) }()

	syncCtx, syncCancel := context.WithTimeout(ctx, 30*time.Second)
	defer syncCancel()
	if !mgr.GetCache().WaitForCacheSync(syncCtx) {
		fmt.Fprintf(os.Stderr, "manager cache failed to sync within 30s\n")
		mgrCancel()
		return 1
	}

	rc := m.Run()

	mgrCancel()
	select {
	case <-mgrDone:
	case <-time.After(5 * time.Second):
	}
	return rc
}
