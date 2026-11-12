// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	utilyaml "k8s.io/apimachinery/pkg/util/yaml"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/controller"
)

type fileList []string

func (f *fileList) String() string     { return fmt.Sprint(*f) }
func (f *fileList) Set(v string) error { *f = append(*f, v); return nil }

// decodeStrict rejects a field the type does not have, which the API server would prune.
func decodeStrict(raw []byte, into any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	return dec.Decode(into)
}

// runValidate checks every Agent in the files against the AgentProfile it names, with the
// checks the controller makes before it applies anything. It reads no cluster: CRD defaults
// and CEL rules are the API server's (`kubectl apply --dry-run=server`).
func runValidate(args []string, stdin io.Reader, out io.Writer) error {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	var files fileList
	fs.Var(&files, "f", "file with Agent and AgentProfile documents, or - for stdin (repeatable)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if len(files) == 0 {
		return errors.New("usage: operator validate -f <file> [-f <file>...]")
	}
	profiles := map[string]achv1alpha1.AgentProfile{}
	var agents []achv1alpha1.Agent
	for _, name := range files {
		in := stdin
		if name != "-" {
			f, err := os.Open(name)
			if err != nil {
				return err
			}
			defer f.Close()
			in = f
		}
		dec := utilyaml.NewYAMLOrJSONDecoder(in, 4096)
		for {
			var raw json.RawMessage
			if err := dec.Decode(&raw); err == io.EOF {
				break
			} else if err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
			var meta struct{ Kind string }
			if err := json.Unmarshal(raw, &meta); err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
			switch meta.Kind {
			case "AgentProfile":
				var p achv1alpha1.AgentProfile
				if err := decodeStrict(raw, &p); err != nil {
					return fmt.Errorf("%s: AgentProfile: %w", name, err)
				}
				profiles[p.Namespace+"/"+p.Name] = p
			case "Agent":
				var a achv1alpha1.Agent
				if err := decodeStrict(raw, &a); err != nil {
					return fmt.Errorf("%s: Agent: %w", name, err)
				}
				agents = append(agents, a)
			case "":
			default:
				return fmt.Errorf("%s: kind %s is not Agent or AgentProfile", name, meta.Kind)
			}
		}
	}
	if len(agents) == 0 {
		return errors.New("no Agent in the files")
	}
	for _, a := range agents {
		p, ok := profiles[a.Namespace+"/"+a.Spec.ProfileRef.Name]
		if !ok {
			return fmt.Errorf("Agent %s: AgentProfile %q of namespace %q is not in the files", a.Name, a.Spec.ProfileRef.Name, a.Namespace)
		}
		if a.UID == "" { // the API server assigns it; the rendered config needs one
			a.UID = "00000000-0000-0000-0000-000000000000"
		}
		if err := controller.Validate(p, a); err != nil {
			return fmt.Errorf("Agent %s: %w", a.Name, err)
		}
		fmt.Fprintf(out, "Agent %s: ok\n", a.Name)
	}
	return nil
}
