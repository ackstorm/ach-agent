// SPDX-License-Identifier: MIT

package openshell

import "testing"

func TestImageRelease(t *testing.T) {
	for image, want := range map[string]string{
		"ghcr.io/x/runtime:0.1.23":        "0.1.23",
		"ghcr.io/x/runtime:v0.1.23":       "0.1.23",
		"ghcr.io/x/runtime:dev":           "",
		"ghcr.io/x/runtime":               "",
		"localhost:5000/runtime":          "",
		"ghcr.io/x/runtime@sha256:abc123": "",
	} {
		if got := imageRelease(image); got != want {
			t.Errorf("imageRelease(%q) = %q, want %q", image, got, want)
		}
	}
}
