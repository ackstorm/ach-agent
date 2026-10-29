// SPDX-License-Identifier: MIT

package render

import (
	"crypto/sha256"
	"encoding/json"
	"os"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

func compileSchema(t *testing.T, path string) *jsonschema.Schema {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open schema: %v", err)
	}
	defer f.Close()
	doc, err := jsonschema.UnmarshalJSON(f)
	if err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	c := jsonschema.NewCompiler()
	if err := c.AddResource(path, doc); err != nil {
		t.Fatalf("add schema resource: %v", err)
	}
	s, err := c.Compile(path)
	if err != nil {
		t.Fatalf("compile schema: %v", err)
	}
	return s
}

const (
	vendoredWorkspaceV2Schema      = "testdata/ach-workspace-config-v2.schema.json"
	vendoredPRReviewRuntime        = "testdata/pr-review-runtime.json"
	vendoredConfigCanonicalization = "testdata/config-canonicalization.json"
)

// TestWorkspaceV2Schema_NoDrift fails if the vendored workspace-v2 schema differs from the
// coordinating runtime-schema-v1 worker's generated copy. Unlike the retired
// TestSchema_NoDrift (the agent-config-v1 contract, which tolerated a missing/pre-addendum
// upstream), this is the live contract; it skips only when no sibling ach-runtime checkout exists (CI). The runtime
// worker's report/commit confirming final GREEN was still pending at vendor time (see
// task-2-ach-report.md); if the upstream file moves, re-vendor and record the new hash there.
func TestWorkspaceV2Schema_NoDrift(t *testing.T) {
	upstream := "../../../ach-runtime/docs/schemas/ach-workspace-config-v2.schema.json"
	up, err := os.ReadFile(upstream)
	if os.IsNotExist(err) {
		// CI has no sibling ach-runtime checkout; scripts/dev.sh mounts it locally.
		t.Skipf("sibling ach-runtime checkout not present (%v)", err)
	}
	if err != nil {
		t.Fatalf("read upstream workspace-v2 schema: %v", err)
	}
	vend, err := os.ReadFile(vendoredWorkspaceV2Schema)
	if err != nil {
		t.Fatalf("read vendored schema: %v", err)
	}
	if sha256.Sum256(up) != sha256.Sum256(vend) {
		t.Fatalf("vendored workspace-v2 schema drifted from %s — re-copy it", upstream)
	}
}

// TestWorkspaceV1Fixture_ConformsToSchema validates the coordinating runtime-schema-v1
// worker's own pr-review-runtime.json sample against the vendored schema — a consistency
// check on the coordination artifacts themselves, independent of this repo's renderer
// (which does not yet produce this wire shape; see task-2-ach-report.md).
func TestWorkspaceV1Fixture_ConformsToSchema(t *testing.T) {
	schema := compileSchema(t, vendoredWorkspaceV2Schema)
	f, err := os.ReadFile(vendoredPRReviewRuntime)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var v any
	if err := json.Unmarshal(f, &v); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	if err := schema.Validate(v); err != nil {
		t.Fatalf("pr-review-runtime.json violates ach-workspace-config-v2:\n%v", err)
	}
}

// TestConfigCanonicalization_FixtureLoads is a presence/shape smoke test on the coordinating
// runtime-schema-v1 worker's JCS canonicalization corpus: it confirms the vendored file is
// well-formed and each case carries the fields the Go canonicalizer will need to validate
// against (canonicalHex, sha256Hex) once that implementation lands (contract §5, pinned to
// the RFC 8785 Go implementation named in config-canonicalization-proposal.md — NOT a
// json.Marshal-map-sort approximation). Implementing the canonicalizer itself is pending;
// see task-2-ach-report.md.
func TestConfigCanonicalization_FixtureLoads(t *testing.T) {
	f, err := os.ReadFile(vendoredConfigCanonicalization)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var doc struct {
		Cases []struct {
			Name         string          `json:"name"`
			Input        json.RawMessage `json:"input"`
			CanonicalHex string          `json:"canonicalHex"`
			SHA256Hex    string          `json:"sha256Hex"`
			RawJSON      json.RawMessage `json:"rawJson"`
			RawBase64    string          `json:"rawBase64"`
			Error        bool            `json:"error"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(f, &doc); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("canonicalization corpus has no cases")
	}
	for _, c := range doc.Cases {
		if c.Name == "" {
			t.Fatalf("case %+v missing a name", c)
		}
		// Two case shapes: a success case canonicalizes input to canonicalHex/sha256Hex;
		// an error case (rawJson or rawBase64 + error) documents input the canonicalizer
		// must reject (duplicate keys, lone surrogates, NaN/Infinity, out-of-range numbers,
		// invalid UTF-8) rather than silently accept.
		switch {
		case c.Error:
			if len(c.RawJSON) == 0 && c.RawBase64 == "" {
				t.Fatalf("error case %q has no rawJson/rawBase64", c.Name)
			}
		default:
			if len(c.Input) == 0 || c.CanonicalHex == "" || c.SHA256Hex == "" {
				t.Fatalf("success case %q missing input/canonicalHex/sha256Hex", c.Name)
			}
		}
	}
}
