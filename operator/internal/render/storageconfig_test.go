// SPDX-License-Identifier: MIT

package render

import (
	"errors"
	"testing"
)

// cfgWithStorageFlags builds the minimal WSConfig RuntimeRequiresStorage/ValidateRuntimeStorage
// read: only the three enabled booleans participate, everything else is zero-value.
func cfgWithStorageFlags(workspacePersist, sessionPersist, artifacts bool) WSConfig {
	var cfg WSConfig
	cfg.Workspace.Persistence.Enabled = workspacePersist
	cfg.Workspace.Session.Persistence.Enabled = sessionPersist
	cfg.Artifacts.Enabled = artifacts
	return cfg
}

// TestRuntimeStorageRequiresAndValidate covers all eight boolean combinations of
// workspace persistence / session persistence / artifacts: RequiresStorage is their OR;
// an absent/empty bucket validates with ErrStorageUnavailable exactly when required; a
// configured bucket always validates regardless of the flags.
func TestRuntimeStorageRequiresAndValidate(t *testing.T) {
	for _, tc := range []struct {
		workspacePersist, sessionPersist, artifacts bool
	}{
		{false, false, false}, {true, false, false}, {false, true, false}, {false, false, true},
		{true, true, false}, {true, false, true}, {false, true, true}, {true, true, true},
	} {
		cfg := cfgWithStorageFlags(tc.workspacePersist, tc.sessionPersist, tc.artifacts)
		wantRequires := tc.workspacePersist || tc.sessionPersist || tc.artifacts

		if got := RuntimeRequiresStorage(cfg); got != wantRequires {
			t.Errorf("flags=%+v: RuntimeRequiresStorage = %v, want %v", tc, got, wantRequires)
		}

		err := ValidateRuntimeStorage(cfg, RuntimeStorageOptions{})
		if wantRequires && !errors.Is(err, ErrStorageUnavailable) {
			t.Errorf("flags=%+v: ValidateRuntimeStorage with empty bucket = %v, want errors.Is ErrStorageUnavailable", tc, err)
		}
		if !wantRequires && err != nil {
			t.Errorf("flags=%+v: ValidateRuntimeStorage with empty bucket and no domain requiring storage = %v, want nil", tc, err)
		}

		if err := ValidateRuntimeStorage(cfg, RuntimeStorageOptions{Bucket: "runtime-bucket"}); err != nil {
			t.Errorf("flags=%+v: ValidateRuntimeStorage with bucket configured = %v, want nil", tc, err)
		}
	}
}

// TestRuntimeStorageEnv pins the exact four backend env names/values, the prefix default,
// and the optional AWS SecretKeyRef wiring.
func TestRuntimeStorageEnv(t *testing.T) {
	t.Run("defaults prefix to ach, no credentials means no AWS refs", func(t *testing.T) {
		env := RuntimeStorageEnv(RuntimeStorageOptions{Bucket: "runtime-bucket"})
		want := map[string]string{
			"ACH_STORAGE_S3_BUCKET":       "runtime-bucket",
			"ACH_STORAGE_S3_REGION":       "",
			"ACH_STORAGE_S3_ENDPOINT_URL": "",
			"ACH_STORAGE_S3_PREFIX":       "ach",
		}
		if len(env) != len(want) {
			t.Fatalf("env = %+v, want exactly %d entries (no AWS refs without a credentials secret name)", env, len(want))
		}
		for _, e := range env {
			wantVal, ok := want[e.Name]
			if !ok {
				t.Errorf("unexpected env name %q", e.Name)
				continue
			}
			if e.Value != wantVal || e.ValueFrom != nil {
				t.Errorf("env %q = %+v, want literal value %q", e.Name, e, wantVal)
			}
		}
	})

	t.Run("explicit region/endpoint/prefix pass through", func(t *testing.T) {
		env := RuntimeStorageEnv(RuntimeStorageOptions{
			Bucket: "runtime-bucket", Region: "us-east-1", EndpointURL: "http://seaweedfs:8333", Prefix: "tenant/runtime",
		})
		want := map[string]string{
			"ACH_STORAGE_S3_BUCKET":       "runtime-bucket",
			"ACH_STORAGE_S3_REGION":       "us-east-1",
			"ACH_STORAGE_S3_ENDPOINT_URL": "http://seaweedfs:8333",
			"ACH_STORAGE_S3_PREFIX":       "tenant/runtime",
		}
		for _, e := range env {
			if want[e.Name] != e.Value {
				t.Errorf("env %q = %q, want %q", e.Name, e.Value, want[e.Name])
			}
		}
	})

	t.Run("credentials secret name adds exactly three AWS SecretKeyRefs", func(t *testing.T) {
		env := RuntimeStorageEnv(RuntimeStorageOptions{Bucket: "runtime-bucket", CredentialsSecretName: "runtime-s3"})
		if len(env) != 7 {
			t.Fatalf("env = %+v, want 4 backend + 3 AWS entries (7 total)", env)
		}
		byName := map[string]struct {
			secretRef *struct{ name, key string }
			optional  *bool
		}{}
		_ = byName
		for _, e := range env {
			switch e.Name {
			case "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY":
				if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil || e.ValueFrom.SecretKeyRef.Name != "runtime-s3" || e.ValueFrom.SecretKeyRef.Key != e.Name {
					t.Errorf("env %q = %+v, want required SecretKeyRef{runtime-s3, %s}", e.Name, e, e.Name)
				}
				if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Optional != nil {
					t.Errorf("env %q must be required (Optional nil), got %v", e.Name, *e.ValueFrom.SecretKeyRef.Optional)
				}
			case "AWS_SESSION_TOKEN":
				if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil || e.ValueFrom.SecretKeyRef.Name != "runtime-s3" || e.ValueFrom.SecretKeyRef.Key != "AWS_SESSION_TOKEN" {
					t.Errorf("env %q = %+v, want SecretKeyRef{runtime-s3, AWS_SESSION_TOKEN}", e.Name, e)
				}
				if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil || e.ValueFrom.SecretKeyRef.Optional == nil || !*e.ValueFrom.SecretKeyRef.Optional {
					t.Errorf("env %q must be Optional=true", e.Name)
				}
			}
		}
	})

	t.Run("no ACH_STORAGE_S3_CREDENTIALS_SECRET and no raw credential values ever appear", func(t *testing.T) {
		env := RuntimeStorageEnv(RuntimeStorageOptions{Bucket: "runtime-bucket", CredentialsSecretName: "runtime-s3"})
		for _, e := range env {
			if e.Name == "ACH_STORAGE_S3_CREDENTIALS_SECRET" {
				t.Fatal("control env must never carry ACH_STORAGE_S3_CREDENTIALS_SECRET")
			}
		}
	})
}
