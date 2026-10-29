// SPDX-License-Identifier: MIT

package render

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strconv"

	"github.com/cyberphone/json-canonicalization/go/src/webpki.org/jsoncanonicalizer"
)

// maxSafeConfigNumber is the binary64-safe-integer bound (2^53-1) shared with the Python
// producer's numeric profile (contract §5): a number beyond it is rejected rather than
// silently losing precision across languages.
const maxSafeConfigNumber = 9007199254740991

// ComputeConfigVersion returns the contract §5 configVersion: 64 lowercase hex characters,
// the SHA-256 of the RFC 8785 (JCS) canonical bytes of the wire object with its top-level
// configVersion member removed. v must already be the fully-resolved wire document (every
// producer-side default applied) and must marshal through encoding/json — this function
// does the canonicalization and hashing only, it does not resolve defaults.
//
// Pinned to github.com/cyberphone/json-canonicalization
// @19d51d7fe467d4706a3ff08adf8a748f29fc21e0, exactly as specified by
// config-canonicalization-proposal.md (ach-agent repo, .superpowers/sdd/2026-09-30-
// workspace-runtime/). That package does not itself enforce the shared numeric profile
// (contract §5: binary64-safe-integer bound) — canonicalizeJCS checks it on the decoded
// value before canonicalizing. Verified against the
// ../ach-agent/tests/config/fixtures/config-canonicalization.json corpus in
// configversion_test.go.
func ComputeConfigVersion(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", fmt.Errorf("marshal: %w", err)
	}
	canon, err := canonicalizeJCS(raw)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canon)
	return hex.EncodeToString(sum[:]), nil
}

// canonicalizeJCS is the SHARED production primitive behind ComputeConfigVersion and the
// configversion_test.go corpus runner. It decodes raw with ordinary encoding/json (object
// keys: last value wins on a duplicate, matching the Python producer's ordinary json.loads —
// neither side enforces duplicate-key rejection), enforces the shared numeric profile on the
// decoded value, then hands the bytes (minus the top-level configVersion member) to the
// pinned JCS transformer.
func canonicalizeJCS(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var root any
	if err := dec.Decode(&root); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	// dec.More() only checks the next byte is not ']'/'}', so "{}]", "{} }" and "{}] garbage"
	// pass it: the decoded root is then remarshaled without ever seeing that suffix. A second
	// Decode must hit end of input (io.EOF); a decoded value or any other error is trailing data.
	if err := dec.Decode(new(json.RawMessage)); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("trailing data after root value")
		}
		return nil, fmt.Errorf("trailing data after root value: %w", err)
	}
	m, ok := root.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("configVersion target must be a JSON object")
	}
	if err := validateNumberBounds(root); err != nil {
		return nil, err
	}
	delete(m, "configVersion")
	withoutVersion, err := json.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("remarshal without configVersion: %w", err)
	}
	canon, err := jsoncanonicalizer.Transform(withoutVersion)
	if err != nil {
		return nil, fmt.Errorf("JCS canonicalize: %w", err)
	}
	return canon, nil
}

// validateNumberBounds recursively enforces the shared numeric profile (contract §5): every
// number must be finite and within ±9007199254740991, the binary64-safe-integer bound shared
// with the Python producer's _check_number. NaN/Infinity tokens are already rejected by
// encoding/json's ordinary JSON grammar before this runs.
func validateNumberBounds(v any) error {
	switch val := v.(type) {
	case map[string]any:
		for _, sub := range val {
			if err := validateNumberBounds(sub); err != nil {
				return err
			}
		}
	case []any:
		for _, item := range val {
			if err := validateNumberBounds(item); err != nil {
				return err
			}
		}
	case json.Number:
		f, err := strconv.ParseFloat(val.String(), 64)
		if err != nil {
			return fmt.Errorf("number %s out of binary64 range: %w", val, err)
		}
		if f > maxSafeConfigNumber || f < -maxSafeConfigNumber {
			return fmt.Errorf("number %s exceeds the safe-integer bound ±%d", val, int64(maxSafeConfigNumber))
		}
	}
	return nil
}
