# Local agent

`ach-agent run` runs an agent from a short YAML manifest on your machine: the same harness, engine and
capability (an ACH environment, or a standalone model and MCP servers) as the cluster, in one container,
with no Kubernetes and no S3. The directory you launch
it from is the agent's workspace, and the engine's TUI opens on its session.

## Install

The host needs Docker and, optionally, `ach-cli`. Copy the script onto your `PATH`:

```bash
install -m 0755 runtime/bin/ach-agent ~/.local/bin/ach-agent
```

It runs `ghcr.io/ackstorm/ach-agent-local:latest`; set `ACH_AGENT_IMAGE` to use another image.

## Authoring flow

1. Create an ACH environment in the Hub with the MCPs, skills, prompts and artifacts the agent needs.
2. Get a credential for it: `ach-cli login`, or an `ek-` key in `ACH_KEY` (see Credentials).
3. Write `agent.yaml` and, optionally, its prompt (`AGENTS.md`).
4. `cd` into the directory the agent works on, then run `ach-agent run --config <path>/agent.yaml`.
5. Iterate: edit the manifest or prompt, quit the TUI, run again.

With `engine: pi` the TUI is the agent loop itself, so it opens once the warm-up turn has finished and the
harness has stopped its RPC process; it resumes the same session.

The harness hydrates the environment at boot and warms the session with one turn before the TUI opens,
so a bad environment, model or credential fails before you type anything.

Quit the opencode TUI with `ctrl+c` twice, or `/exit`; quit the pi TUI with `ctrl+d` on an empty editor, or
`/quit`. That stops the agent and removes the container
(`ach-agent: stopped`). `/exit` is not a pi command: it goes to the model as a prompt, and `ctrl+c` in pi
only clears the editor. The `Continue opencode mini -s ses_…` line opencode prints on exit and the
"To resume this session" hint pi prints are their own hints and do not apply here: the session lived in the
removed container, so the next `ach-agent run` starts a new one.

## Manifest

With an `ach:` block the model and tools come from an ACH environment:

```yaml
ach:
  profile: default             # optional: ach-cli profile
  environment: support-prod    # required
model:                         # required
  name: gemini.gemini-flash-latest
  type: gemini                 # openai | gemini | anthropic
  thinking: {enabled: true, effort: medium}   # optional
  limits: {context: 524288, output: 65536}    # optional: overrides the limits ACH reports
  params: {temperature: 0.2}                  # optional
mcpServers:                    # optional: added to the environment's MCP servers
  - name: github
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: {env: GITHUB_MCP_AUTH}   # export GITHUB_MCP_AUTH="Bearer …"
prompt:                        # optional: exactly one of file | text
  file: ./AGENTS.md
exclude:                       # optional
  mcpServers: [mcp-slack]
  tools: [zoho_desk_delete_ticket]
```

Without `ach:` the agent is standalone: it calls the model endpoint directly and has only the
`mcpServers` listed, with no hydration (no skills, prompts or artifacts from an environment):

```yaml
model:
  name: claude-sonnet-5-5
  type: anthropic
  baseUrl: https://api.anthropic.com/v1        # required without ach
  headers:
    x-api-key: {env: ANTHROPIC_API_KEY}
  limits: {context: 200000, output: 64000}     # optional: enables size-based compaction
mcpServers:
  - name: github
    url: https://api.githubcopilot.com/mcp/
    headers: {Authorization: {env: GITHUB_MCP_AUTH}}
prompt: {text: You review pull requests.}
exclude: {tools: [delete_repository]}         # skills are not allowed without ach
```

A model served on your machine (Ollama, vLLM, LM Studio) is reachable from the container as
`host.docker.internal`:

```yaml
model: {name: qwen3-coder, type: openai, baseUrl: "http://host.docker.internal:11434/v1",
        limits: {context: 32000, output: 8000}}
```

| Field | Required | Default | Becomes |
|---|---|---|---|
| `ach.profile` | no | ach-cli's default profile | the Hub URL and credential, read on the host |
| `ach.environment` | with `ach` | | `capability.ach.environment` |
| `model` | yes | | `capability.ach.model` (`name`, `type`, `params`, `thinking`, `limits`) or `capability.standalone.model` (also `baseUrl`, `headers`) |
| `mcpServers` (`name`, `url`, `headers`) | no | none | `capability.ach.mcpServers` or `capability.standalone.mcpServers` |
| `prompt.file` / `prompt.text` | no | none | `prompt.system` as text; a file is read and inlined |
| `exclude` (`mcpServers`, `tools`, `skills`) | no | nothing | `capability.filter.exclude` |
| `engine` (`opencode` or `pi`) | no | `opencode` | `engine.type` |
| `denyTools` | no | none | `engine.denyTools` |
| `limits` (any wire `limits` key, e.g. `maxSteps`, `maxInvocationSeconds`) | no | `maxSteps: 50`, `maxInvocationSeconds: 600` | `limits` |

A header is `{env: NAME}` (a secret read from your shell) or `{value: …}` (a literal). The value of
`NAME` is sent as is, so an `Authorization` header needs its `Bearer ` prefix inside the variable. With
`ach:`, an extra MCP server is never sent the ACH credential, and a name the environment already has is
an error at start.

`model.limits` sets the context and output window the engine compacts against. With `ach:` it
wins over the limits ACH reports for the model; set it only when the model's real window differs.

`denyTools` hides native engine tools from the model: opencode names such as `[websearch, webfetch]`, or pi
names such as `[bash, write]`; a name that is not a pi tool is an error at start with pi. It is not a
boundary: `bash` reaches what a denied tool would. With opencode, `execute` (Code Mode) is always denied, so
MCP tools are called directly.

Unknown keys are an error. Paths resolve relative to the manifest. Compaction and timeouts are
internal defaults of `run`. `limits.maxSteps` caps the engine steps of one turn: at the cap opencode
disables tools and the turn ends without a reply (pi: see Engines), so a long task (a report, a multi-deck edit) needs
a higher value, e.g. `limits: {maxSteps: 400, maxInvocationSeconds: 3600}`. There are no channels:
paste what a channel would send into the TUI.

## Engines

`engine: opencode` (default) and `engine: pi` run the same manifest. pi is PiG, a Go build of the pi coding
agent, shipped as `/opt/pi/pig` in the `ghcr.io/ackstorm/ach-agent-execution-pi:<version>` image (181 MB
against 368 MB for `execution`). With `engine: pi` the TUI is the engine itself, so it opens only after the
warm-up turn, resumes the same session, and takes over from the harness.

| Topic | opencode | pi |
|---|---|---|
| `limits.maxSteps` | Native, exact | Soft, can overshoot by a turn or two, ends `failed` with no final answer |
| File tools outside the workspace | Denied by `external_directory` | Allowed, bounded only by the boundary below |
| `model.type` | openai, gemini, anthropic | openai only; others fail at start with a config error |
| `ach-agent run` TUI | Client of the running engine | Is the engine; limits and the MCP check do not apply after the warm-up turn |
| Reasoning tokens in usage | Reported | Null |

`limits.maxSteps` and `maxInvocationSeconds` apply to the warm-up turn only; in the TUI a person is at the
keyboard. A dead MCP server fails the warm-up turn, but pi continues without it in later TUI turns.

A crash during a tool leaves the tool's effect done once, and the next prompt carries a "No result
provided" tool result for it. The harness does not retry, so a model that reads that result may redo the
action.

## Security

| Mode | Boundary |
|---|---|
| Agent in OpenShell | The OpenShell policy |
| `ach-agent run` | The Docker container: `--user` of the caller, only `$PWD` and `/agent` (read-only) mounted, unrestricted network |
| `denyTools` | Not a boundary: `bash` reaches what a denied tool would |

pi loads no extensions beyond its built-in MCP client, and MCP traffic goes through the harness facades.

## Credentials

With `ach:`:

- `ACH_URL` and `ACH_KEY` both set in the environment (CI): used as they are, ach-cli is not called.
- Otherwise the script reads `ach.profile` from the manifest and takes the Hub URL from
  `ach-cli profile show <profile>` and the credential from `ach-cli token --profile <profile>`.

Standalone manifests need neither ach-cli nor `ACH_*`. In both modes every `{env: NAME}` a header names
must be set in your shell; `run` stops at start and lists any that are not.

Credentials reach the container by variable name only; their values never appear on a command line,
and the TUI process does not get them.

A login profile's credential is a session token that lives 60 minutes, and the harness reads it once at
boot. `run` prints its expiry to the terminal just before the TUI takes over the screen:

```text
warning: the ACH session token expires at HH:MM UTC (in N min); restart ach-agent run after that
```

After that time model calls fail: restart `ach-agent run`, or use a key profile (`ek-`), which prints no
warning. There is no token refresh.

## Workspace

- The workspace is the launch directory, mounted at the same path inside the container, so paths the
  agent reports are host paths. The script refuses to run from `/`.
- The container runs as your user, so files the agent writes belong to you.
- Files are kept between runs; the conversation starts fresh each run.
- `.ach-state/` in the workspace holds the hydrated skills, prompts and artifacts. Add it to
  `.gitignore`.
- The agent can change or delete any file in the directory, including the manifest and prompt when
  they live there. Run it from a working directory, never from `$HOME`.
