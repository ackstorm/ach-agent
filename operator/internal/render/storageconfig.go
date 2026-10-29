// SPDX-License-Identifier: MIT

package render

import (
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
)

const defaultRuntimeStoragePrefix = "ach"

// RuntimeStorageOptions is the operator/chart-level global Storage backend configuration
// (root D1 decision): bucket/region/endpoint/prefix plus an optional namespace-local
// credentials Secret name. NEVER per-Agent, NEVER per-AgentProfile — sourced from
// operator env/Helm (cmd/ach/cmd/operator.go), the same pattern as DefaultAchBaseURL.
type RuntimeStorageOptions struct {
	Bucket                string
	Region                string
	EndpointURL           string
	Prefix                string
	CredentialsSecretName string
}

// ErrStorageUnavailable is the identifiable sentinel ValidateRuntimeStorage returns when a
// resolved WSConfig requires the backend but the operator has no bucket configured —
// callers map it to condition reason StorageUnavailable. This checks REQUIRED
// CONFIGURATION, never cloud reachability; an actual SDK/operation failure at runtime is a
// separate, truthful error, never masked as this one and never silently downgraded to a
// volatile fallback.
var ErrStorageUnavailable = errors.New("agentrender: runtime storage backend is not configured (operator env/Helm)")

// RuntimeRequiresStorage reports whether cfg's resolved workspace/session/artifacts policy
// needs the backend at all — the OR of the three enabled domains. All-disabled skips the
// backend dependency entirely.
func RuntimeRequiresStorage(cfg WSConfig) bool {
	return cfg.Workspace.Persistence.Enabled || cfg.Workspace.Session.Persistence.Enabled || cfg.Artifacts.Enabled
}

// ValidateRuntimeStorage checks required configuration only: a nonempty bucket when any
// domain requires storage. A configured bucket is configuration evidence only — this never
// contacts the backend.
func ValidateRuntimeStorage(cfg WSConfig, options RuntimeStorageOptions) error {
	if !RuntimeRequiresStorage(cfg) {
		return nil
	}
	if options.Bucket == "" {
		return fmt.Errorf("%w: configure runtime.storage.s3.bucket (operator Helm values), or disable workspace/session persistence and artifacts", ErrStorageUnavailable)
	}
	return nil
}

// RuntimeStorageEnv emits the control pod's private backend env: the four backend
// coordinates always, plus — only when CredentialsSecretName is set — three AWS
// SecretKeyRefs into that namespace-local Secret (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
// required, AWS_SESSION_TOKEN optional). Never a raw credential value and never
// ACH_STORAGE_S3_CREDENTIALS_SECRET itself in control env — callers gate this on a
// successfully resolved cfg that actually requires storage (RuntimeRequiresStorage).
func RuntimeStorageEnv(options RuntimeStorageOptions) []corev1.EnvVar {
	prefix := options.Prefix
	if prefix == "" {
		prefix = defaultRuntimeStoragePrefix
	}
	env := []corev1.EnvVar{
		{Name: "ACH_STORAGE_S3_BUCKET", Value: options.Bucket},
		{Name: "ACH_STORAGE_S3_REGION", Value: options.Region},
		{Name: "ACH_STORAGE_S3_ENDPOINT_URL", Value: options.EndpointURL},
		{Name: "ACH_STORAGE_S3_PREFIX", Value: prefix},
	}
	if options.CredentialsSecretName == "" {
		return env
	}
	optionalToken := true
	ref := func(key string, optional *bool) corev1.EnvVar {
		return corev1.EnvVar{Name: key, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: options.CredentialsSecretName}, Key: key, Optional: optional,
		}}}
	}
	return append(env, ref("AWS_ACCESS_KEY_ID", nil), ref("AWS_SECRET_ACCESS_KEY", nil), ref("AWS_SESSION_TOKEN", &optionalToken))
}
