// SPDX-License-Identifier: MIT

package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLeaderElectionNamespace(t *testing.T) {
	sa := filepath.Join(t.TempDir(), "namespace")
	if err := os.WriteFile(sa, []byte("ach-system\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(t.TempDir(), "none")
	for name, tc := range map[string]struct {
		env, file, want string
		fail            bool
	}{
		"env wins":        {"ns1", sa, "ns1", false},
		"service account": {"", sa, "ach-system", false},
		"neither":         {"", missing, "", true},
	} {
		got, err := leaderElectionNamespace(tc.env, tc.file)
		if (err != nil) != tc.fail || got != tc.want {
			t.Errorf("%s: got (%q, %v), want (%q, fail=%v)", name, got, err, tc.want, tc.fail)
		}
	}
}
