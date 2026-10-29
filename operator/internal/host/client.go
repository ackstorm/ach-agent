// SPDX-License-Identifier: MIT

// Package host is the operator side of ach-runtime contract v2 section 5: it
// long-polls a control unit's /host/v1 for desired state and reconciles one execution
// StatefulSet per Workspace against it. Messages and status codes are defined only in
// contract v2 section 5.3; this file is a client of them, not a restatement.
package host

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const maxBody = 8 << 20 // a listing carries up to 4 env values of 8 KiB per Workspace

var (
	// ErrUnauthorized: the host token was rejected; the caller re-reads the key.
	ErrUnauthorized = errors.New("host: host token rejected (401)")
	// ErrStaleEpoch: the inventory named an epoch that is no longer the control unit's.
	ErrStaleEpoch = errors.New("host: inventory epoch is stale (409)")
)

// Instance is a listed registration. InstanceID is null until the first successful probe.
type Instance struct {
	InstanceID *string `json:"instanceId"`
	BaseURL    string  `json:"baseUrl"`
	State      string  `json:"state"`
}

// Launch is everything needed to start an execution unit (contract 5.5). It holds no secret.
type Launch struct {
	RuntimeRelease string            `json:"runtimeRelease"`
	Port           int32             `json:"port"`
	Command        []string          `json:"command"`
	Env            map[string]string `json:"env"`
}

// Workspace is one entry of the desired state. Desired is "running" or "released".
type Workspace struct {
	Name         string    `json:"name"`
	WorkspaceRef string    `json:"workspaceRef"`
	Desired      string    `json:"desired"`
	Launch       Launch    `json:"launch"`
	Instance     *Instance `json:"instance"`
}

// Listing is the full desired state. While Adopting is true Workspaces is empty and is
// NOT desired state: nothing may be removed because it is unlisted.
type Listing struct {
	Cursor       string      `json:"cursor"`
	RuntimeEpoch string      `json:"runtimeEpoch"`
	Adopting     bool        `json:"adopting"`
	Workspaces   []Workspace `json:"workspaces"`
}

// InventoryUnit is one running unit reported to a control unit that is adopting.
type InventoryUnit struct {
	Name         string `json:"name"`
	WorkspaceRef string `json:"workspaceRef"`
	BaseURL      string `json:"baseUrl"`
}

// RegisterOutcome is the meaning of a PUT /workspaces/{name}/instance answer.
type RegisterOutcome int

const (
	Registered        RegisterOutcome = iota // 202 or 200
	NotDesired                               // 409 {"error":"not_desired"}
	AlreadyRegistered                        // 409 {"error":"registered"}
)

// HostToken is HMAC-SHA256(key, "host:"+agentUID) as lowercase hex. key is the UTF-8 bytes of
// the ach-key-<uid> Secret value exactly as stored: the hex text is NOT decoded (contract 5.2).
func HostToken(key []byte, agentUID string) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("host:" + agentUID))
	return hex.EncodeToString(mac.Sum(nil))
}

// ControlBaseURL is the host API of an agent's control unit. ControlName hashes and
// truncates long or dotted agent names: never build agent-<name> by hand.
func ControlBaseURL(agentName, namespace string) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:8081/host/v1", render.ControlName(agentName), namespace)
}

// Client talks to one control unit's /host/v1.
type Client struct {
	BaseURL string // ControlBaseURL(...)
	Token   string // HostToken(...)
	HTTP    *http.Client
}

// callTimeout bounds every call but the long poll, so a half-open connection cannot stall the loop.
const callTimeout = 30 * time.Second

func (c *Client) do(ctx context.Context, method, path string, body []byte) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	h := c.HTTP
	if h == nil {
		h = http.DefaultClient
	}
	return h.Do(req)
}

func statusErr(resp *http.Response, what string) error {
	if resp.StatusCode == http.StatusUnauthorized {
		return ErrUnauthorized
	}
	return fmt.Errorf("host: %s: unexpected status %d", what, resp.StatusCode)
}

// List long-polls the desired state: held until it changes after the cursor `after`, or
// waitSeconds passes. An empty `after` answers at once.
func (c *Client) List(ctx context.Context, after string, waitSeconds int) (*Listing, error) {
	q := url.Values{"waitSeconds": {strconv.Itoa(waitSeconds)}}
	if after != "" {
		q.Set("after", after)
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(waitSeconds)*time.Second+15*time.Second)
	defer cancel()
	resp, err := c.do(ctx, http.MethodGet, "/workspaces?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, statusErr(resp, "list workspaces")
	}
	var l Listing
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBody)).Decode(&l); err != nil {
		return nil, fmt.Errorf("host: decode listing: %w", err)
	}
	if l.RuntimeEpoch == "" || l.Cursor == "" { // fail closed: never read a bare body as "nothing desired"
		return nil, errors.New("host: listing without runtimeEpoch or cursor")
	}
	return &l, nil
}

// Register reports a started unit's base URL.
func (c *Client) Register(ctx context.Context, name, baseURL string) (RegisterOutcome, error) {
	ctx, cancel := context.WithTimeout(ctx, callTimeout)
	defer cancel()
	body, _ := json.Marshal(map[string]string{"baseUrl": baseURL})
	resp, err := c.do(ctx, http.MethodPut, "/workspaces/"+url.PathEscape(name)+"/instance", body)
	if err != nil {
		return 0, err
	}
	defer func() { _ = resp.Body.Close() }()
	switch resp.StatusCode {
	case http.StatusOK, http.StatusAccepted:
		return Registered, nil
	case http.StatusConflict:
		var e struct {
			Error string `json:"error"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&e)
		switch e.Error {
		case "not_desired":
			return NotDesired, nil
		case "registered":
			return AlreadyRegistered, nil
		}
		return 0, fmt.Errorf("host: register %s: unexpected 409 body %q", name, e.Error)
	}
	return 0, statusErr(resp, "register "+name)
}

// Delete reports that a unit has stopped. instanceID is the one from the last listing;
// nil when the listing carried none. It always answers 204.
func (c *Client) Delete(ctx context.Context, name string, instanceID *string) error {
	ctx, cancel := context.WithTimeout(ctx, callTimeout)
	defer cancel()
	path := "/workspaces/" + url.PathEscape(name) + "/instance"
	if instanceID != nil && *instanceID != "" {
		path += "?" + url.Values{"instanceId": {*instanceID}}.Encode()
	}
	resp, err := c.do(ctx, http.MethodDelete, path, nil)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusNoContent {
		return statusErr(resp, "delete instance "+name)
	}
	return nil
}

// Inventory reports every unit this host runs for the agent, once per runtime epoch.
func (c *Client) Inventory(ctx context.Context, epoch string, units []InventoryUnit) error {
	ctx, cancel := context.WithTimeout(ctx, callTimeout)
	defer cancel()
	if units == nil {
		units = []InventoryUnit{} // the body is units:[] , never null
	}
	body, _ := json.Marshal(struct {
		RuntimeEpoch string          `json:"runtimeEpoch"`
		Units        []InventoryUnit `json:"units"`
	}{epoch, units})
	resp, err := c.do(ctx, http.MethodPut, "/inventory", body)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	switch resp.StatusCode {
	case http.StatusOK, http.StatusAccepted:
		return nil
	case http.StatusConflict:
		return ErrStaleEpoch
	}
	return statusErr(resp, "inventory")
}
