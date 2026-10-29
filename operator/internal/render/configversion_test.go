// SPDX-License-Identifier: MIT

package render

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

// canonicalizeForTest calls canonicalizeJCS — the SAME production primitive
// ComputeConfigVersion uses — directly, so the corpus's rejection cases prove the actual
// production path, not a parallel reimplementation of it (the Important review finding: the
// two pipelines had drifted apart).
func canonicalizeForTest(t *testing.T, input []byte) ([]byte, error) {
	t.Helper()
	return canonicalizeJCS(input)
}

// TestJCS_CanonicalizationCorpus verifies canonicalizeJCS — ordinary encoding/json decoding
// plus the shared numeric-profile check (contract §5), then the pinned
// github.com/cyberphone/json-canonicalization Transform — against every case in the
// coordinating runtime-schema-v1 worker's corpus: success cases (exact canonicalHex/sha256Hex
// match) and rejection cases (NaN/Infinity, out-of-range numbers, trailing data). Duplicate
// keys and raw surrogate-escape validity are no longer rejected by either language's ordinary
// JSON decoding, so the corpus no longer carries those cases.
func TestJCS_CanonicalizationCorpus(t *testing.T) {
	raw, err := os.ReadFile(vendoredConfigCanonicalization)
	if err != nil {
		t.Fatalf("read corpus: %v", err)
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
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal corpus: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("corpus has no cases")
	}
	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			if c.Error {
				var input []byte
				if len(c.RawJSON) > 0 {
					// rawJson in the corpus is itself a JSON string literal containing the
					// raw (possibly malformed) JSON text to feed the canonicalizer.
					var s string
					if err := json.Unmarshal(c.RawJSON, &s); err != nil {
						t.Fatalf("corpus rawJson must be a JSON string: %v", err)
					}
					input = []byte(s)
				} else if c.RawBase64 != "" {
					decoded, err := base64.StdEncoding.DecodeString(c.RawBase64)
					if err != nil {
						t.Fatalf("corpus rawBase64 invalid: %v", err)
					}
					input = decoded
				} else {
					t.Fatal("error case has neither rawJson nor rawBase64")
				}
				if _, err := canonicalizeForTest(t, input); err == nil {
					t.Fatalf("expected %s to be rejected, canonicalized without error", c.Name)
				}
				return
			}
			got, err := canonicalizeForTest(t, c.Input)
			if err != nil {
				t.Fatalf("Transform: %v", err)
			}
			if hex.EncodeToString(got) != c.CanonicalHex {
				t.Fatalf("canonical bytes mismatch:\n got:  %s\n want: %s", hex.EncodeToString(got), c.CanonicalHex)
			}
			sum := sha256.Sum256(got)
			if hex.EncodeToString(sum[:]) != c.SHA256Hex {
				t.Fatalf("sha256 mismatch: got %s want %s", hex.EncodeToString(sum[:]), c.SHA256Hex)
			}
		})
	}
}

// TestJCS_ClosingDelimiterTrailingDataRejected pins the I1 correction: dec.More() only
// checked the next byte was not ']'/'}', so a suffix starting with one of those passed
// unnoticed. canonicalizeJCS now requires a second Decode to hit io.EOF.
func TestJCS_ClosingDelimiterTrailingDataRejected(t *testing.T) {
	for _, input := range []string{"{}]", "{} }", "{}] garbage"} {
		t.Run(input, func(t *testing.T) {
			if _, err := canonicalizeForTest(t, []byte(input)); err == nil {
				t.Fatalf("expected %q to be rejected as trailing data, canonicalized without error", input)
			}
		})
	}
}

func TestJCS_TrailingWhitespaceAccepted(t *testing.T) {
	got, err := canonicalizeForTest(t, []byte("{}  \n\t"))
	if err != nil {
		t.Fatalf("trailing whitespace after the root value must be accepted: %v", err)
	}
	if string(got) != "{}" {
		t.Fatalf("canonical bytes = %q, want %q", got, "{}")
	}
}

func TestComputeConfigVersion_ExcludesTopLevelOnly(t *testing.T) {
	type nested struct {
		ConfigVersion string `json:"configVersion"`
	}
	v := struct {
		A             int    `json:"a"`
		ConfigVersion string `json:"configVersion"`
		Model         nested `json:"model"`
	}{A: 1, ConfigVersion: "ignored", Model: nested{ConfigVersion: "nested"}}

	got, err := ComputeConfigVersion(v)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 64 {
		t.Fatalf("configVersion len = %d, want 64", len(got))
	}
	// Changing the top-level configVersion must not change the hash (it is excluded).
	v.ConfigVersion = "different"
	got2, err := ComputeConfigVersion(v)
	if err != nil {
		t.Fatal(err)
	}
	if got != got2 {
		t.Fatal("top-level configVersion must not affect the hash")
	}
	// Changing the NESTED configVersion (model.params.configVersion in the real schema)
	// must change the hash — only the top-level member is excluded.
	v.Model.ConfigVersion = "changed"
	got3, err := ComputeConfigVersion(v)
	if err != nil {
		t.Fatal(err)
	}
	if got3 == got2 {
		t.Fatal("nested configVersion must be retained and affect the hash")
	}
}
