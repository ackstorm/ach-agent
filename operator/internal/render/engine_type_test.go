package render

import (
	"encoding/json"
	"testing"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func TestEngineType(t *testing.T) {
	compaction := &v1.CompactionSpec{Auto: ptr(true), Keep: &v1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))}
	render := func(engine *v1.EngineSpec) map[string]any {
		b, err := RenderEngineV1(engine, nil)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := json.Marshal(b)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatal(err)
		}
		return m
	}
	if got := render(&v1.EngineSpec{Type: ptr("pi"), Compaction: compaction})["type"]; got != "pi" {
		t.Errorf("engine.type = %v, want pi", got)
	}
	if _, ok := render(&v1.EngineSpec{Compaction: compaction})["type"]; ok {
		t.Error("engine.type rendered although unset")
	}
	profile := &v1.EngineSpec{Type: ptr("pi")}
	if got := ResolveEngine(&v1.EngineSpec{}, profile); got.Type == nil || *got.Type != "pi" {
		t.Errorf("ResolveEngine dropped the profile type: %v", got.Type)
	}
	if got := ResolveEngine(&v1.EngineSpec{Type: ptr("opencode")}, profile); *got.Type != "opencode" {
		t.Errorf("agent type does not override: %v", *got.Type)
	}
}
