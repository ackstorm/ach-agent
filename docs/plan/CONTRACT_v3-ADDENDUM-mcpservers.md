# CONTRACT_v3 ADDENDUM — `mcpServers` (harness-managed MCP servers)

> **Status:** proposed (2026-07-07). Supersedes the `engine.repoCheckout` block shipped in the
> gitlab-repo-checkout branch. Moves repo-checkout out of `engine` into a new **top-level
> `mcpServers`** map that also carries operator-declared **local** and **remote** MCP servers
> passed through to opencode. Consolidate into `CONTRACT_v3.md` §2 when landed.

---

## 1. Why

Two facts forced the move:

1. **repo-checkout is not an engine knob.** It is a *server the harness hosts*. Nesting it under
   `engine` (how-we-run-opencode) conflated two concerns.
2. **Agents need to bring their own MCP servers.** Beyond ACH-fronted egress (hydrate), operators
   want to wire extra MCP tools — a local stdio server (`mcp/filesystem` in a container) or a
   plain remote MCP — that opencode connects to directly. `ackbot-process` already did this
   (`src/config.py:MCPServer` → `opencode_extensions._normalize_mcp_server`).

So: one top-level `mcpServers` map, discriminated by `type`, covering both **internal/harness-hosted**
(`repoCheckout`) and **passthrough** (`local`, `remote`) servers.

### Two classes of MCP, two origins (unchanged distinction, now explicit)

| Class | Origin | Who serves it | In config |
|---|---|---|---|
| **ACH-fronted external** (gitlab, slack…) | `hydrate` → `runtime.mcpServers[{id,endpoint}]` | ACH Forwarder, proxied via localhost | **no** (comes from hydrate) |
| **Harness-managed** (repoCheckout, local, remote) | **`mcpServers`** (operator) | harness hosts / opencode connects | **yes** (this block) |

`mcpServers` (config) and `runtime.mcpServers` (hydrate) are different namespaces — no collision.

---

## 2. Rendered config delta (`/etc/ach-agent/config.json`)

**Remove** `engine.repoCheckout`. **Add** a top-level `mcpServers` object (map keyed by server
name — the MCP-ecosystem convention: opencode.json / `.mcp.json` / Claude Desktop all key by name):

```jsonc
{
  "engine": { /* … repoCheckout REMOVED … */ },

  "mcpServers": {

    "repo-checkout": {                    // INTERNAL: the harness HOSTS it (FastMCP facade + ek_)
      "type": "repoCheckout",
      "repoCheckout": {                   //   params nested (it is a built-in "special" wiring)
        "sourceMcpServerId": "mcp-gitlab-ro",  // which hydrated runtime.mcpServers[].id serves the
        //                                        gitlab://{project}/archive/{ref} resource the
        //                                        harness reads (with the ek_, harness-side).
        "tmpBase": "/tmp/gitlab",         //   parent dir for per-checkout mkdtemp dirs (default)
        "ttlSeconds": 3600                //   stale-checkout sweep TTL (repoCheckout-only)
      }
    },

    "filesystem": {                       // PASSTHROUGH local: opencode LAUNCHES it (stdio subprocess)
      "type": "local",
      "command": "docker",
      "args": ["run","-i","--rm",
               "--mount","type=bind,src=/data/desktop,dst=/projects/desktop",
               "mcp/filesystem","/projects"],
      "env": []                           //   env NAMES to forward (never the ek_); optional
    },

    "other": {                            // PASSTHROUGH remote: opencode CONNECTS directly
      "type": "remote",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:OTHER_MCP_TOKEN}" }  // ${env:NAME} refs, not values
    }

  }
}
```

Presence in the map = enabled. Omit an entry → not wired. No `enabled` flag.

---

## 3. Wiring semantics — what the harness does per `type`

| `type` | Who runs it | `opencode.json` `mcp.<name>` entry | Auth |
|---|---|---|---|
| **`repoCheckout`** | **harness** (FastMCP facade) | `{type:"remote", url:"http://localhost/mcp/repo-checkout"}` | harness injects `ek_` as `x-ach-key` (opencode never sees it) |
| **`local`** | **opencode** (stdio subprocess) | `{type:"local", command:[command, ...args], environment:{NAME:val}}` | env (NAMES resolved at launch) |
| **`remote`** | **opencode** (direct MCP client) | `{type:"remote", url, headers}` | operator `headers` (`${env:NAME}` resolved at write) |

- `repoCheckout` is the only **harness-hosted** entry: the harness reads the archive resource
  itself (opencode discards MCP resource blobs) and exposes `checkout_repo(project, ref, subpath?)`.
  Fail-soft; TTL-swept; `head_sha` from the gitlab channel; prompt hint gated (see §9 of CONTRACT_v3).
- `local` / `remote` are **passthrough**: opencode connects directly, NOT through the ACH localhost
  proxy — they are "bring-your-own-MCP". Mirrors `ackbot-process._normalize_mcp_server`
  (stdio→`local`, http/streamable→`remote`; unknown fields stripped — opencode validates strictly).

### Security (honest)

- **`remote`**: opencode connects directly, so its auth **header lands in `opencode.json`** (opencode
  needs it) — no `ek_`-style hiding. Header/env values are `${env:NAME}` refs; the harness resolves
  at write time; the operator wires those env vars into the pod from a `Secret`. The co-resident
  agent CAN read that credential. If that is unacceptable, front the server via ACH (hydrate) instead.
- **`local`**: same-uid subprocess; `--mount …,ro` and `capability.filter.exclude.mcpServers` still
  apply. The `ek_`/`ACH_SECRET_*` are stripped from the forwarded env (same rule as `engine.forwardEnv`).

---

## 4. Harness schema (Pydantic v2) — drop-in for `config/schema.py`

Validated (union + negatives). Add these classes; wire `mcp_servers` into `AgentConfig`; delete
`RepoCheckoutBlock` and the `EngineBlock.repo_checkout` field.

```python
# ---------------------------------------------------------------------------
# mcpServers — harness-managed MCP servers (CONTRACT_v3 §2a)
# ---------------------------------------------------------------------------


class RepoCheckoutParams(BaseModel):
    """Params for the harness-hosted repoCheckout facade (the `checkout_repo` tool)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The hydrated runtime.mcpServers[].id whose endpoint serves the
    # gitlab://{project}/archive/{ref} resource the harness reads (harness-side, with the ek_).
    source_mcp_server_id: str = Field(alias="sourceMcpServerId")
    tmp_base: str = Field(default="/tmp/gitlab", alias="tmpBase")
    ttl_seconds: float = Field(default=3600.0, ge=0, alias="ttlSeconds")


class RepoCheckoutServer(BaseModel):
    """INTERNAL: the harness HOSTS this MCP (FastMCP facade), injecting the ek_."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["repoCheckout"]
    repo_checkout: RepoCheckoutParams = Field(alias="repoCheckout")


class LocalMcpServer(BaseModel):
    """PASSTHROUGH: opencode LAUNCHES this as a stdio subprocess."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["local"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)  # env NAMES only; never the ek_


class RemoteMcpServer(BaseModel):
    """PASSTHROUGH: opencode CONNECTS directly to a remote MCP endpoint."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["remote"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)  # values are ${env:NAME} refs


# Strict discriminated union on `type` (mirror of the Memory union). Named *Config to avoid
# clashing with engine.hydrate.McpServer (the hydrated {id,endpoint} external server).
McpServerConfig = Annotated[
    RepoCheckoutServer | LocalMcpServer | RemoteMcpServer, Field(discriminator="type")
]
```

In `AgentConfig` (and delete `engine.repoCheckout`):

```python
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
```

Regenerate the frozen artifact: `uv run python scripts/gen_schema.py`
(`docs/schemas/agent-config-v1.schema.json`; drift-guarded by `tests/config/test_schema_artifact.py`).

---

## 5. Operator seam (ach-runtime CRD `Agent`)

CRD authoring uses a **list with `name`** (k8s idiom, like `channels`); the operator renders
list → the config **map**. Tagged union = `type` enum + optional per-type sub-blocks + CEL
(mirror of `PromptSourceSpec`).

```yaml
spec:
  mcpServers:
    - name: repo-checkout
      type: repoCheckout
      repoCheckout:
        sourceMcpServerId: mcp-gitlab-ro   # must exist in capabilityProfileRef's MCP set
        tmpBase: /tmp/gitlab               # optional
        ttlSeconds: 3600                   # optional
    - name: filesystem
      type: local
      local:
        command: docker
        args: [run, -i, --rm, --mount, "type=bind,src=/data,dst=/projects/data", mcp/filesystem, /projects]
        # env: [SOME_VAR]
    - name: other
      type: remote
      remote:
        url: https://mcp.example.com/mcp
        headers: { Authorization: "Bearer ${env:OTHER_MCP_TOKEN}" }
```

```go
// +kubebuilder:validation:XValidation:rule="(self.type=='repoCheckout' && has(self.repoCheckout)) || (self.type=='local' && has(self.local)) || (self.type=='remote' && has(self.remote))",message="mcpServers[]: type-specific block required (VAL-03)"
type McpServerSpec struct {
    Name         string            `json:"name"`                    // +required
    Type         string            `json:"type"`                    // +enum=repoCheckout;local;remote
    RepoCheckout *RepoCheckoutSpec `json:"repoCheckout,omitempty"`
    Local        *LocalMcpSpec     `json:"local,omitempty"`
    Remote       *RemoteMcpSpec    `json:"remote,omitempty"`
}
type RepoCheckoutSpec struct {
    SourceMcpServerID string `json:"sourceMcpServerId"`
    TmpBase           string `json:"tmpBase,omitempty"`
    TTLSeconds        *int64 `json:"ttlSeconds,omitempty"`
}
type LocalMcpSpec struct {
    Command string   `json:"command"`
    Args    []string `json:"args,omitempty"`
    Env     []string `json:"env,omitempty"`
}
type RemoteMcpSpec struct {
    URL     string            `json:"url"`
    Headers map[string]string `json:"headers,omitempty"`
}
// AgentSpec: MCPServers []McpServerSpec `json:"mcpServers,omitempty"`
```

**Render rules:**
- list → map keyed by `name`.
- **cross-validate** `repoCheckout.sourceMcpServerId` against the MCP set the `capabilityProfileRef`
  exposes (the hydrate set). CEL can't (cross-object) → reconciler/webhook check, clear error if absent.
- `remote.headers` / `local.env` values render as `${env:NAME}` refs — NAMES, never values; the
  operator wires the env from a `Secret` (same pattern as `webhook.auth.secretRef`).

---

## 6. Migration (breaking; no live agents render `engine.repoCheckout` yet)

1. Delete `EngineBlock.repo_checkout` + `RepoCheckoutBlock`.
2. Add the `mcpServers` union + `AgentConfig.mcp_servers`.
3. `main.py`: replace the `engine.repoCheckout` resolve with an iteration over `cfg.mcp_servers` —
   `repoCheckout` → start `RepoCheckoutFacade` (params from `repoCheckout`, resolve
   `sourceMcpServerId` against the hydrated set); `local`/`remote` → normalize into `opencode.json`
   (ackbot `_normalize_mcp_server` port).
4. CONTRACT_v3 §2: replace the `engine.repoCheckout` doc with a `mcpServers` section; §9 already
   names the three harness-hosted MCPs.
5. Regenerate the frozen schema.
