// SPDX-License-Identifier: MIT

package host

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const (
	testKey = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	testUID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
)

func TestHostToken_KnownAnswer(t *testing.T) {
	const want = "71b872fe8ceb7cd93d81b390517b7124a57dab020154a8565ccf273fae02a8ef"
	if got := HostToken([]byte(testKey), testUID); got != want {
		t.Fatalf("HostToken = %s, want %s (key must be used as UTF-8 text, not hex-decoded)", got, want)
	}
}

func TestControlBaseURL_UsesControlName(t *testing.T) {
	for _, name := range []string{"gitlab-reviewer", "dotted.agent.name"} {
		want := "http://" + render.ControlName(name) + ".ns.svc.cluster.local:8081/host/v1"
		if got := ControlBaseURL(name, "ns"); got != want {
			t.Errorf("ControlBaseURL(%q) = %q, want %q", name, got, want)
		}
	}
}

// route answers one request and records it.
type recorded struct{ method, path, query, auth, body string }

func serve(t *testing.T, status int, body string) (*Client, *recorded) {
	t.Helper()
	rec := &recorded{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		*rec = recorded{r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("Authorization"), string(b)}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	return &Client{BaseURL: srv.URL + "/host/v1", Token: "tok"}, rec
}

func TestList_DecodesNullInstanceAndNullInstanceID(t *testing.T) {
	c, rec := serve(t, 200, `{"cursor":"e.4","runtimeEpoch":"e","adopting":false,"workspaces":[
	 {"name":"w1","workspaceRef":"r1","desired":"running","launch":{"runtimeRelease":"0.1.22","port":8080,"command":["python","-m","ach_runtime","execution"],"env":{"A":"b"}},"instance":null},
	 {"name":"w2","workspaceRef":"r2","desired":"released","launch":{"port":8080,"command":["x"],"env":{}},"instance":{"instanceId":null,"baseUrl":"http://w2-0.w2.ns.svc.cluster.local:8080","state":"probing"}}]}`)
	l, err := c.List(context.Background(), "e.3", 30)
	if err != nil {
		t.Fatal(err)
	}
	if rec.method != "GET" || rec.path != "/host/v1/workspaces" || rec.query != "after=e.3&waitSeconds=30" || rec.auth != "Bearer tok" {
		t.Errorf("request = %+v", *rec)
	}
	if l.RuntimeEpoch != "e" || len(l.Workspaces) != 2 || l.Workspaces[0].Instance != nil {
		t.Fatalf("listing = %+v", l)
	}
	if l.Workspaces[0].Launch.Env["A"] != "b" || l.Workspaces[0].Launch.Port != 8080 {
		t.Errorf("launch = %+v", l.Workspaces[0].Launch)
	}
	if in := l.Workspaces[1].Instance; in == nil || in.InstanceID != nil || in.State != "probing" {
		t.Errorf("instance = %+v, want probing with null instanceId", in)
	}
}

func TestList_OmitsAfterWhenEmptyAndMapsUnauthorized(t *testing.T) {
	c, rec := serve(t, 401, "")
	if _, err := c.List(context.Background(), "", 0); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("err = %v, want ErrUnauthorized", err)
	}
	if rec.query != "waitSeconds=0" {
		t.Errorf("query = %q, want waitSeconds=0 only", rec.query)
	}
	c, _ = serve(t, 503, "")
	if _, err := c.List(context.Background(), "", 0); err == nil || errors.Is(err, ErrUnauthorized) {
		t.Fatalf("503 must be a plain error, got %v", err)
	}
}

func TestRegister_Outcomes(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
		want   RegisterOutcome
	}{
		{202, "", Registered},
		{200, "", Registered},
		{409, `{"error":"not_desired"}`, NotDesired},
		{409, `{"error":"registered"}`, AlreadyRegistered},
	} {
		c, rec := serve(t, tc.status, tc.body)
		got, err := c.Register(context.Background(), "w1", "http://w1-0.w1.ns.svc.cluster.local:8080")
		if err != nil || got != tc.want {
			t.Errorf("status %d: got (%v, %v), want %v", tc.status, got, err, tc.want)
		}
		var body map[string]string
		_ = json.Unmarshal([]byte(rec.body), &body)
		if rec.method != "PUT" || rec.path != "/host/v1/workspaces/w1/instance" || body["baseUrl"] != "http://w1-0.w1.ns.svc.cluster.local:8080" {
			t.Errorf("request = %+v", *rec)
		}
	}
	c, _ := serve(t, 422, "")
	if _, err := c.Register(context.Background(), "w1", "x"); err == nil {
		t.Error("422 must be an error")
	}
}

func TestDelete_PassesInstanceIDOnlyWhenKnown(t *testing.T) {
	c, rec := serve(t, 204, "")
	id := "abc"
	if err := c.Delete(context.Background(), "w1", &id); err != nil {
		t.Fatal(err)
	}
	if rec.method != "DELETE" || rec.path != "/host/v1/workspaces/w1/instance" || rec.query != "instanceId=abc" {
		t.Errorf("request = %+v", *rec)
	}
	if err := c.Delete(context.Background(), "w1", nil); err != nil || rec.query != "" {
		t.Errorf("nil instanceId: err=%v query=%q, want no query", err, rec.query)
	}
}

func TestInventory_Statuses(t *testing.T) {
	units := []InventoryUnit{{Name: "w1", WorkspaceRef: "r1", BaseURL: "http://w1-0.w1.ns.svc.cluster.local:8080"}}
	for _, status := range []int{200, 202} {
		c, rec := serve(t, status, "")
		if err := c.Inventory(context.Background(), "e", units); err != nil {
			t.Errorf("status %d: %v", status, err)
		}
		if rec.method != "PUT" || rec.path != "/host/v1/inventory" ||
			rec.body != `{"runtimeEpoch":"e","units":[{"name":"w1","workspaceRef":"r1","baseUrl":"http://w1-0.w1.ns.svc.cluster.local:8080"}]}` {
			t.Errorf("request = %+v", *rec)
		}
	}
	c, _ := serve(t, 409, "")
	if err := c.Inventory(context.Background(), "old", nil); !errors.Is(err, ErrStaleEpoch) {
		t.Errorf("409 err = %v, want ErrStaleEpoch", err)
	}
	c, rec := serve(t, 202, "")
	_ = c.Inventory(context.Background(), "e", nil)
	if rec.body != `{"runtimeEpoch":"e","units":[]}` {
		t.Errorf("empty inventory body = %s, want units:[] (never null)", rec.body)
	}
}

func TestList_EmptyEpochOrCursorFailsClosed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{}`)) }))
	defer srv.Close()
	if _, err := (&Client{BaseURL: srv.URL, Token: "t"}).List(context.Background(), "", 0); err == nil {
		t.Fatal("a listing without runtimeEpoch and cursor must be an error, not an empty desired state")
	}
}

func TestClient_GatewayPutsAvailability(t *testing.T) {
	for _, available := range []bool{false, true} {
		c, rec := serve(t, http.StatusNoContent, "")
		if err := c.Gateway(context.Background(), available); err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		_ = json.Unmarshal([]byte(rec.body), &body)
		if rec.method != http.MethodPut || rec.path != "/host/v1/gateway" || body["available"] != available || rec.auth != "Bearer tok" {
			t.Fatalf("request = %+v body = %v, want PUT /host/v1/gateway available=%v", rec, body, available)
		}
	}
}

func TestClient_GatewayRejections(t *testing.T) {
	c, _ := serve(t, http.StatusUnprocessableEntity, `{"error":"bad body"}`)
	if err := c.Gateway(context.Background(), true); err == nil || errors.Is(err, ErrUnauthorized) {
		t.Fatalf("422 err = %v, want a plain failure", err)
	}
	c, _ = serve(t, http.StatusUnauthorized, "")
	if err := c.Gateway(context.Background(), true); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("401 err = %v, want ErrUnauthorized", err)
	}
}
