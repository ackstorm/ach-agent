import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { fileURLToPath } from "node:url"
import test from "node:test"

import plugin from "../../plugins/managed-config/index.js"

const baseFixture = (overrides = {}) => ({
  managedAgent: "build",
  maxSteps: 8,
  providers: { managed: { baseURL: "http://127.0.0.1:9000", models: {} } },
  ...overrides,
})

function capturingContext(fixture, agents = { build: { system: ["native base"], instructions: ["native instructions"] }, other: { system: ["other base"] } }) {
  const hooks = {}
  const registrations = { agent: 0, provider: 0, mcp: 0 }
  const providers = []
  const ctx = {
    options: { fixture },
    agents,
    hooks,
    registrations,
    providers,
    agent: {
      async transform(edit) {
        registrations.agent++
        edit({ update(id, mutate) { mutate(agents[id]) } })
      },
    },
    provider: {
      async transform(edit) {
        registrations.provider++
        edit({ add(value) { providers.push(value) } })
      },
    },
    mcp: {
      async transform(edit) {
        registrations.mcp++
        edit({ set() {} })
      },
    },
    session: {
      async hook(name, handler) { hooks[name] = handler },
    },
  }
  return ctx
}

test("provider registrations preserve catalog values and select native transports", async (t) => {
  for (const [providerID, packageName] of [
    ["openai", "@opencode/ai/providers/openai-compatible"],
    ["gemini", "@opencode/ai/providers/google"],
    ["anthropic", "@opencode/ai/providers/anthropic"],
    ["managed", "@opencode/ai/providers/openai-compatible"],
  ]) {
    await t.test(providerID, async () => {
      const provider = {
        baseURL: "https://facade.example/t/model-token",
        apiKey: "local-proxy",
        models: { "vendor/model": { name: "Model name", tool_call: true, limit: { output: 2048 } } },
      }
      const ctx = capturingContext(baseFixture({ providers: { [providerID]: provider } }))
      await plugin.setup(ctx)

      assert.equal(ctx.providers.length, 1)
      assert.deepEqual(ctx.providers[0], {
        info: { id: providerID, name: providerID, activation: "enabled", package: packageName,
          settings: { baseURL: provider.baseURL, apiKey: provider.apiKey } },
        models: [{
          id: "vendor/model", modelID: "vendor/model", providerID,
          name: "Model name", capabilities: { tools: true, input: ["text"], output: ["text"] },
          variants: [], time: { released: 0 },
          cost: [{ input: 0, output: 0, cache: { read: 0, write: 0 } }],
          status: "active", enabled: true, limit: { context: 128000, output: 2048 },
        }],
      })
    })
  }
})

test("request options are forwarded unchanged only for the managed agent", async () => {
  const requestOptions = { thinkingConfig: { thinkingLevel: "high" }, temperature: 0.25 }
  const ctx = capturingContext(baseFixture({ requestOptions }))
  await plugin.setup(ctx)
  const managed = { agent: "build", options: { existing: true } }
  const other = { agent: "other", options: { existing: true } }

  await ctx.hooks.context(managed)
  await ctx.hooks.context(other)

  assert.deepEqual(managed.options, { existing: true, ...requestOptions })
  assert.deepEqual(other.options, { existing: true })
})

test("replace sets the managed agent system prompt and preserves initial instructions", async () => {
  const ctx = capturingContext(baseFixture({ systemPrompt: "managed system", promptCompose: "replace" }))

  await plugin.setup(ctx)

  assert.deepEqual(ctx.agents.build.system, "managed system")
  assert.deepEqual(ctx.agents.build.instructions, ["native instructions"])
  assert.deepEqual(ctx.agents.other, { system: ["other base"] })
})

test("append adds one SystemPart to each fresh managed-agent context", async () => {
  const ctx = capturingContext(baseFixture({ systemPrompt: "managed addition", promptCompose: "append" }))
  await plugin.setup(ctx)

  assert.deepEqual(ctx.agents.build.system, ["native base"])
  assert.equal(typeof ctx.hooks.context, "function")
  for (let i = 0; i < 2; i++) {
    const event = { agent: "build", system: [{ type: "text", text: "native request" }] }
    await ctx.hooks.context(event)
    assert.deepEqual(event.system, [
      { type: "text", text: "native request" },
      { type: "text", text: "managed addition" },
    ])
  }
})

test("absent or empty prompt text leaves the native base and request untouched", async (t) => {
  for (const promptFields of [{}, { systemPrompt: "", promptCompose: "replace" }]) {
    await t.test(JSON.stringify(promptFields), async () => {
      const ctx = capturingContext(baseFixture(promptFields))
      await plugin.setup(ctx)

      assert.deepEqual(ctx.agents.build.system, ["native base"])
      assert.equal(ctx.hooks.context, undefined)
    })
  }
})

test("append leaves other agents unchanged", async () => {
  const ctx = capturingContext(baseFixture({ systemPrompt: "managed addition", promptCompose: "append" }))
  await plugin.setup(ctx)
  const event = { agent: "other", system: [{ type: "text", text: "other request" }] }

  await ctx.hooks.context(event)

  assert.deepEqual(event.system, [{ type: "text", text: "other request" }])
})

test("unresolved required file or ACH prompt rejects before registration", async (t) => {
  for (const source of ["file", "ach"]) {
    await t.test(source, async () => {
      const ctx = capturingContext(baseFixture({ systemPromptUnresolved: source }))

      await assert.rejects(plugin.setup(ctx), /unresolved.*system prompt/i)
      assert.deepEqual(ctx.registrations, { agent: 0, provider: 0, mcp: 0 })
    })
  }
})

test("memory tools reach the model for an agent with no prompt block", async () => {
  // The fixture comes from the real engine/config.py:to_fixture, so a missing promptCompose
  // there (neither append nor replace) is caught here, not only at the plugin boundary.
  const root = fileURLToPath(new URL("../..", import.meta.url))
  const script = `
import json
from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine.config import to_fixture
wire = json.load(open("tests/config/fixtures/pr-review-runtime.json"))
wire.pop("prompt")
config = RuntimeConfig.model_validate(wire)
public = public_config(config, engine_env=config.engine.env)
catalog = {
    "providers": {public.model.type: {"baseURL": "http://facade", "models": {public.model.name: {}}}},
    "mcp": {},
    "memoryToolsSpec": "use memory",
}
print(json.dumps(to_fixture(public, managed_agent="build", catalog=catalog)))
`
  const python = process.env.PYTHON ?? "python3"
  const fixture = JSON.parse(execFileSync(python, ["-c", script], {
    cwd: root, env: { ...process.env, PYTHONPATH: `${root}src` },
  }))
  const ctx = capturingContext(fixture)
  await plugin.setup(ctx)

  assert.equal(typeof ctx.hooks.context, "function")
  const event = { agent: "build", system: [{ type: "text", text: "native request" }], options: {} }
  await ctx.hooks.context(event)
  assert.deepEqual(event.system, [
    { type: "text", text: "native request" },
    { type: "text", text: "## Memory Tools\nuse memory" },
  ])
})

test("file tools outside the workspace are denied, except OpenCode's own output files", async (t) => {
  const previous = process.env.XDG_DATA_HOME
  process.env.XDG_DATA_HOME = "/s/home/.local/share"
  t.after(() => {
    if (previous === undefined) delete process.env.XDG_DATA_HOME
    else process.env.XDG_DATA_HOME = previous
  })
  const ctx = capturingContext(baseFixture())

  await plugin.setup(ctx)

  assert.deepEqual(
    ctx.agents.build.permissions.filter((rule) => rule.action === "external_directory"),
    [
      { action: "external_directory", resource: "*", effect: "deny" },
      { action: "external_directory", resource: "/s/home/.local/share/opencode/tool-output/*", effect: "allow" },
      { action: "external_directory", resource: "/s/home/.local/share/opencode/shell/*/*", effect: "allow" },
    ],
  )
})

test("denyTools names become deny rules on the managed agent only", async () => {
  const ctx = capturingContext(baseFixture({ denyTools: ["question", "execute"] }))

  await plugin.setup(ctx)

  const denied = (agent) => (agent.permissions ?? []).filter((r) => r.action === "question" || r.action === "execute")
  assert.deepEqual(denied(ctx.agents.build), [
    { action: "question", resource: "*", effect: "deny" },
    { action: "execute", resource: "*", effect: "deny" },
  ])
  assert.deepEqual(denied(ctx.agents.other), [])
})

test("absent denyTools adds no extra rules", async () => {
  const ctx = capturingContext(baseFixture())
  await plugin.setup(ctx)
  assert.equal(ctx.agents.build.permissions.some((r) => r.action === "question"), false)
})
