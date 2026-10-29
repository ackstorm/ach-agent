// SPDX-License-Identifier: MIT

package openshell

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	osv1 "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1"
	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/ackstorm/ach-agent/operator/internal/host"
)

// rebuildEvery bounds how often a client that saw the gateway unavailable is replaced.
const rebuildEvery = 5 * time.Second

// IsUnavailable reports whether err means the gateway could not be reached or did not answer
// in time: gRPC Unavailable or DeadlineExceeded, a call deadline, or a transport error.
func IsUnavailable(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, host.ErrGatewayUnavailable) || osv1.IsUnavailable(err) || osv1.IsDeadlineExceeded(err) ||
		errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	switch status.Code(err) {
	case codes.Unavailable, codes.DeadlineExceeded:
		return true
	}
	var opErr *net.OpError
	return errors.As(err, &opErr)
}

// markUnavailable wraps the gateway so that every unavailable error is also
// host.ErrGatewayUnavailable (the original stays in the chain).
type markUnavailable struct{ sandboxes }

// mark also counts a gRPC Canceled the caller did not ask for (ctx still live) as an outage:
// replacing the shared client closes it under the calls still in flight on it.
func mark(ctx context.Context, err error) error {
	if status.Code(err) == codes.Canceled && ctx.Err() == nil {
		return fmt.Errorf("%w: %w", host.ErrGatewayUnavailable, err)
	}
	if IsUnavailable(err) && !errors.Is(err, host.ErrGatewayUnavailable) {
		return fmt.Errorf("%w: %w", host.ErrGatewayUnavailable, err)
	}
	return err
}

func (m markUnavailable) Create(ctx context.Context, workspace, name string, spec *ostypes.SandboxSpec, opts ...ostypes.CreateOption) (*ostypes.Sandbox, error) {
	s, err := m.sandboxes.Create(ctx, workspace, name, spec, opts...)
	return s, mark(ctx, err)
}

func (m markUnavailable) Get(ctx context.Context, workspace, name string) (*ostypes.Sandbox, error) {
	s, err := m.sandboxes.Get(ctx, workspace, name)
	return s, mark(ctx, err)
}

func (m markUnavailable) ListAll(ctx context.Context, workspace string, opts ...ostypes.ListOptions) ([]*ostypes.Sandbox, error) {
	l, err := m.sandboxes.ListAll(ctx, workspace, opts...)
	return l, mark(ctx, err)
}

func (m markUnavailable) Delete(ctx context.Context, workspace, name string, opts ...ostypes.DeleteOptions) (*ostypes.DeletionResult, error) {
	r, err := m.sandboxes.Delete(ctx, workspace, name, opts...)
	return r, mark(ctx, err)
}

// clientBox is an SDK sandbox client that can be closed when it is replaced.
type clientBox struct {
	osv1.SandboxInterface
	client *osv1.Client
}

func (c clientBox) Close() error { return c.client.Close() }

// Rebuilding holds the current SDK client and replaces it after a call that found the gateway
// unavailable, at most once per `every`: the SDK offers no keepalive or reconnect hook, so a
// connection that died with the gateway pod would otherwise stay dead.
type Rebuilding struct {
	factory func() (sandboxes, error)
	every   time.Duration

	mu    sync.Mutex
	cur   sandboxes
	built time.Time
}

// NewRebuilding builds the client lazily on first use. every 0 replaces on every unavailable call.
func NewRebuilding(factory func() (sandboxes, error), every time.Duration) *Rebuilding {
	return &Rebuilding{factory: factory, every: every}
}

func (r *Rebuilding) client() (sandboxes, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cur == nil {
		c, err := r.factory()
		if err != nil {
			return nil, err
		}
		r.cur, r.built = c, time.Now()
	}
	return r.cur, nil
}

// replace drops c (closing it) unless it was built within the last `every` or is already gone.
// A deadline is not proof of a dead connection when the caller set it itself (a slow delete).
func (r *Rebuilding) replace(c sandboxes, err error, deadlineCounts bool) {
	if !IsUnavailable(err) || (!deadlineCounts && (osv1.IsDeadlineExceeded(err) || errors.Is(err, context.DeadlineExceeded))) {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cur != c || time.Since(r.built) < r.every {
		return
	}
	if closer, ok := c.(interface{ Close() error }); ok {
		_ = closer.Close()
	}
	r.cur = nil
}

func (r *Rebuilding) Create(ctx context.Context, workspace, name string, spec *ostypes.SandboxSpec, opts ...ostypes.CreateOption) (*ostypes.Sandbox, error) {
	c, err := r.client()
	if err != nil {
		return nil, err
	}
	s, err := c.Create(ctx, workspace, name, spec, opts...)
	r.replace(c, err, true)
	return s, err
}

func (r *Rebuilding) Get(ctx context.Context, workspace, name string) (*ostypes.Sandbox, error) {
	c, err := r.client()
	if err != nil {
		return nil, err
	}
	s, err := c.Get(ctx, workspace, name)
	r.replace(c, err, true)
	return s, err
}

func (r *Rebuilding) ListAll(ctx context.Context, workspace string, opts ...ostypes.ListOptions) ([]*ostypes.Sandbox, error) {
	c, err := r.client()
	if err != nil {
		return nil, err
	}
	l, err := c.ListAll(ctx, workspace, opts...)
	r.replace(c, err, true)
	return l, err
}

func (r *Rebuilding) Delete(ctx context.Context, workspace, name string, opts ...ostypes.DeleteOptions) (*ostypes.DeletionResult, error) {
	c, err := r.client()
	if err != nil {
		return nil, err
	}
	res, err := c.Delete(ctx, workspace, name, opts...)
	r.replace(c, err, false)
	return res, err
}
