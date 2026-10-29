// Managed-config plugin for OpenCode v2 (Task 4). Ported verbatim from the Task 1 spike
// (spikes/opencode-v2/managed-config.mjs), which live-verified this plugin surface against
// the pinned @opencode/cli@2.0.20 release — see docs/reports/opencode-v2-compatibility.md.
// Packaged here as a single directory entrypoint (index.js) instead of the spike's
// index.js -> ../managed-config.mjs re-export, which existed only so the spike's Node test
// harness could import the implementation independently of the plugin package shape; a
// runtime plugin has no such caller, so the split buys nothing here.
//
// Real, live-verified v2.0.20 plugin surface used here:
// - `ctx.agent.transform` (steps budget + subagent deny)
// - `ctx.provider.transform` / `editor.add({info, models})` -- alitellm-auth's
//   own `applyV2Config` shape (clients/opencode/index.mjs:534-558); `toModelInfo`
//   below is that file's mapping (id/modelID/capabilities/cost/limit), reused
//   as-is. Dropping `modelID` crashes the real runner (`e.modelID.toLowerCase`).
// - `ctx.mcp.transform` / `editor.set(name, {type:"remote",...})`
// - `ctx.session.hook("model.request", ...)` to tag each outbound request
//   with its real classification (`primary`/`title`/`compaction`/`generate`),
//   and `ctx.session.hook("retry", ...)` to disable transport retries so a
//   probe's request count is deterministic.
// - `ctx.session.hook("context", ...)` to append a managed system prompt to
//   each fresh request when promptCompose is `append`.
//
// Consumes a public fixture via plugin `options` -- the mini-harness translates its
// accepted PublicConfig into this fixture shape (engine/config.py:to_fixture); never a
// hardcoded baseURL/port. A missing or malformed fixture throws from `setup`, which must
// fail the run before any prompt is admitted: a managed deployment must never fall back to
// an unmanaged provider silently.
function toModelInfo(providerID, id, m) {
  const modality = (list) => (Array.isArray(list) && list.length ? list : ["text"])
  return {
    id,
    modelID: id,
    providerID,
    name: m?.name ?? id,
    capabilities: {
      tools: !!m?.tool_call,
      input: modality(m?.modalities?.input),
      output: modality(m?.modalities?.output),
    },
    variants: [],
    time: { released: 0 },
    cost: [
      {
        input: Number(m?.cost?.input ?? 0),
        output: Number(m?.cost?.output ?? 0),
        cache: { read: Number(m?.cost?.cache_read ?? 0), write: 0 },
      },
    ],
    status: "active",
    enabled: true,
    limit: { context: Number(m?.limit?.context ?? 128000), output: Number(m?.limit?.output ?? 8192) },
  }
}

// F1: the managed step budget is the load-bearing loop-safety invariant (contract §4) -- an
// absent, zero, negative, fractional or non-numeric value must reject the whole fixture
// (zero provider registration, zero primary model calls), not silently fall through to
// `agent.steps = undefined`.
function assertFixture(fixture) {
  if (!fixture || typeof fixture !== "object") throw new Error("ach.managed-config: missing public fixture (plugin options.fixture)")
  if (!fixture.providers || Object.keys(fixture.providers).length === 0) throw new Error("ach.managed-config: fixture has no providers")
  for (const [id, p] of Object.entries(fixture.providers)) {
    if (!p.baseURL) throw new Error(`ach.managed-config: provider ${id} missing baseURL`)
  }
  if (!Number.isInteger(fixture.maxSteps) || fixture.maxSteps <= 0) {
    throw new Error(`ach.managed-config: maxSteps must be a positive integer, got ${JSON.stringify(fixture.maxSteps)}`)
  }
  if (fixture.systemPromptUnresolved) {
    throw new Error(`ach.managed-config: unresolved required system prompt (${fixture.systemPromptUnresolved})`)
  }
  return fixture
}

export default {
  id: "ach.managed-config",
  async setup(ctx) {
    const fixture = assertFixture(ctx.options?.fixture)
    const agentID = fixture.managedAgent ?? "build"

    await ctx.agent.transform((editor) => {
      editor.update(agentID, (agent) => {
        agent.steps = fixture.maxSteps
        agent.permissions ??= []
        agent.permissions.push({ action: "subagent", resource: "*", effect: "deny" })
        for (const action of fixture.denyTools ?? []) {
          agent.permissions.push({ action, resource: "*", effect: "deny" })
        }
        // A headless run has nobody to answer OpenCode's default `external_directory: ask`:
        // a file tool (read/glob/grep/edit) outside the workspace would wait forever. Deny
        // it instead, then re-allow OpenCode's own output files (last match wins). The
        // `shell` tool is not guarded by `external_directory`.
        agent.permissions.push({ action: "external_directory", resource: "*", effect: "deny" })
        const data = process.env.XDG_DATA_HOME
        if (data) {
          for (const dir of ["tool-output/*", "shell/*/*"]) {
            agent.permissions.push({ action: "external_directory", resource: `${data}/opencode/${dir}`, effect: "allow" })
          }
        }
        if (fixture.systemPrompt && fixture.promptCompose === "replace") agent.system = fixture.systemPrompt
      })
    })

    if (fixture.requestOptions && Object.keys(fixture.requestOptions).length > 0) {
      await ctx.session.hook("context", (e) => {
        if (e.agent === agentID) e.options = { ...e.options, ...fixture.requestOptions }
      })
    }

    if (fixture.systemPrompt && fixture.promptCompose === "append") {
      await ctx.session.hook("context", (e) => {
        if (e.agent === agentID) e.system.push({ type: "text", text: fixture.systemPrompt })
      })
    }

    await ctx.provider.transform((editor) => {
      for (const [providerID, provider] of Object.entries(fixture.providers)) {
        const models = Object.entries(provider.models ?? {}).map(([id, m]) => toModelInfo(providerID, id, m))
        const packageName = {
          openai: "@opencode/ai/providers/openai-compatible",
          gemini: "@opencode/ai/providers/google",
          anthropic: "@opencode/ai/providers/anthropic",
        }[providerID] ?? "@opencode/ai/providers/openai-compatible"
        editor.add({
          info: { id: providerID, name: providerID, activation: "enabled", package: packageName, settings: { baseURL: provider.baseURL, ...(provider.apiKey !== undefined ? { apiKey: provider.apiKey } : {}) } },
          models,
        })
      }
    })

    if (fixture.mcp) {
      await ctx.mcp.transform((editor) => {
        for (const [name, m] of Object.entries(fixture.mcp)) {
          editor.set(name, { type: "remote", url: m.url, headers: m.headers ?? {}, oauth: false, codemode: false, protocol: "legacy" })
        }
      })
    }

    // Review finding (Minor): the handoff names these as Task 1's test instrumentation, not
    // product policy — disabling native transport retries unconditionally would be an
    // unrequested runtime behavior change. Gated behind an explicit opt-in the production
    // translation (engine/config.py:to_fixture) never sets.
    if (fixture.testInstrumentation) {
      await ctx.session.hook("model.request", (e) => {
        e.headers["x-ach-probe-kind"] = e.kind
        e.headers["x-ach-probe-session"] = e.sessionID
      })
      await ctx.session.hook("retry", (e) => {
        e.decision = { retry: false }
      })
    }
  },
}

export { toModelInfo }
