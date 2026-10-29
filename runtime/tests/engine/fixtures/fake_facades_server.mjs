// Thin control wrapper around the Task 1 spike's proven fake model+MCP fixture
// (tests/fixtures/fake-facades.mjs) — reused as-is, not reimplemented. Spawned as a
// short-lived process by Python tests so the REAL opencode2 binary can point at a
// scripted, loopback model/MCP backend instead of an unreachable HTTPS facade.
//
// argv[2] selects the scenario; prints one JSON line `{modelUrl, mcpUrl}` to stdout once
// both fakes are listening, then stays alive (reading stdin) until killed.
import { createServer } from "node:http"
import { Readable } from "node:stream"
import { startFakeFacades, textChunks } from "../../fixtures/fake-facades.mjs"

const scenario = process.argv[2] ?? "text"
const fixture = await startFakeFacades()
let scenarioText = scenario === "error" ? "" : "pong"
let holdModel = scenario === "hold"
let modelRequests = 0
const heldResponses = []
const mcpCalls = []
let advertisedToolName = null

function latestBusinessTurn(body = {}) {
  const contents = Array.isArray(body.contents) ? body.contents : []
  for (let index = contents.length - 1; index >= 0; index--) {
    const item = contents[index]
    if (item?.role !== "user") continue
    const parts = Array.isArray(item.parts) ? item.parts : []
    // Gemini serializes function responses as user-role contents too; they belong to
    // the existing turn and must not be mistaken for a new business input.
    if (parts.some((part) => part.functionResponse)) continue
    const functionResults = contents.slice(index + 1).reduce((count, later) => (
      count + (Array.isArray(later.parts) ? later.parts.filter((part) => part.functionResponse).length : 0)
    ), 0)
    return { item, functionResults }
  }
  return { item: null, functionResults: 0 }
}

const BUSINESS_PROMPT_PREFIX = "Review the current state of this merge request."

function latestBusinessInput(contents) {
  const plainUser = (item) => item?.role === "user" && Array.isArray(item.parts) && !item.parts.some((part) => part.functionResponse)
  // Native system-update contents are user-role text too; prefer the configured review prompt
  // (tests/config/fixtures/pr-review-runtime.json) and keep the old rule for other prompts.
  const prompt = contents.findLast((item) => plainUser(item) && item.parts.some((part) => typeof part.text === "string" && part.text.startsWith(BUSINESS_PROMPT_PREFIX)))
  return prompt ?? contents.findLast(plainUser) ?? null
}

function latestUserPayload(body = {}) {
  const item = latestBusinessInput(Array.isArray(body.contents) ? body.contents : [])
  if (!item) return {}
  const text = item.parts.filter((part) => typeof part.text === "string").map((part) => part.text).join("\n")
  const match = text.match(/Payload:\s*(\{[\s\S]*\})\s*Repository:/)
  try { return JSON.parse(match?.[1] ?? text) } catch { return {} }
}

function geminiReply(_index, body = {}) {
  if (scenario === "review" || scenario === "loop") {
    const declarations = (body.tools ?? []).flatMap((tool) => tool.functionDeclarations ?? [])
    // Direct (codemode:false) MCP tool: v2.0.20 flattens namespace + tool as `${server}_${tool}`.
    const noteTool = declarations.find((tool) => tool.name === "mcp-gitlab-ro_create_merge_request_note")
    const { functionResults } = latestBusinessTurn(body)
    const payload = latestUserPayload(body)
    const projectId = payload.project?.id ?? 1
    const mergeRequestIid = payload.merge_request?.iid ?? payload.object_attributes?.iid ?? 4
    const shouldCall = scenario === "loop"
      ? functionResults < 2
      : functionResults === 0
    const businessArgs = { project_id: projectId, merge_request_iid: mergeRequestIid, body: scenario === "loop" ? `review-${functionResults + 1}` : "Review complete: one actionable finding." }
    const toolsDisabled = body.toolConfig?.functionCallingConfig?.mode === "NONE"
    const calls = shouldCall && noteTool && !toolsDisabled
      ? [{ name: noteTool.name, args: businessArgs }]
      : null
    if (calls) {
      return { candidates: [{ content: { role: "model", parts: calls.map((call) => ({ functionCall: call })) }, finishReason: "STOP" }] }
    }
  }
  return { candidates: [{ content: { role: "model", parts: [{ text: scenarioText }] }, finishReason: "STOP" }] }
}

if (scenario === "hold") {
  fixture.setModelHandler(() => ({ hold: true, chunks: [] }))
} else if (scenario === "error") {
  fixture.setModelHandler(() => ({ chunks: textChunks("") }))
} else {
  fixture.setModelHandler(() => ({ chunks: textChunks("pong") }))
}

const records = []
const modelProxy = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const raw = Buffer.concat(chunks).toString("utf8")
  let body = {}
  try { body = JSON.parse(raw) } catch {}
  const record = {
    kind: request.headers["x-ach-probe-kind"] ?? "unclassified",
    method: request.method,
    url: request.url,
    body,
    heldModel: holdModel,
    achHeaders: Object.fromEntries(Object.entries(request.headers).filter(([key]) => key.startsWith("x-ach-") || key === "authorization").map(([key, value]) => [key, key === "authorization" ? "<present>" : value])),
    responseClosed: false,
  }
  records.push(record)
  response.on("close", () => { record.responseClosed = true })
  const nativeGemini = request.url.includes(":streamGenerateContent")
  const nativeAnthropic = new URL(request.url, "http://localhost").pathname.endsWith("/messages")
  if (nativeGemini || nativeAnthropic) {
    response.writeHead(200, { "content-type": "text/event-stream" })
    if (holdModel) {
      heldResponses.push({ response, body })
      response.on("close", () => { response.__achClosed = true })
      return
    }
    if (nativeGemini) {
      response.end(`data: ${JSON.stringify(geminiReply(modelRequests++, body))}\n\n`)
      return
    }
    const events = [
      ["message_start", { type: "message_start", message: { id: "msg_fixture", type: "message", role: "assistant", content: [], model: body.model ?? "fixture", stop_reason: null, stop_sequence: null, usage: { input_tokens: 1, output_tokens: 0 } } }],
      ["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }],
      ["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: scenarioText } }],
      ["content_block_stop", { type: "content_block_stop", index: 0 }],
      ["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 1 } }],
      ["message_stop", { type: "message_stop" }],
    ]
    response.end(events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(""))
    return
  }
  const upstream = await fetch(fixture.modelUrl + request.url, {
    method: request.method,
    headers: Object.fromEntries(Object.entries(request.headers).filter(([key]) => key !== "host")),
    body: raw || undefined,
  })
  response.writeHead(upstream.status, Object.fromEntries(upstream.headers))
  if (upstream.body) Readable.fromWeb(upstream.body).pipe(response)
  else response.end()
})
await new Promise((resolve) => modelProxy.listen(0, "127.0.0.1", resolve))
const modelUrl = `http://127.0.0.1:${modelProxy.address().port}/v1`

// Keep the proven fixture's MCP transport/lifecycle handling, while exposing the one
// managed GitLab business tool required by this composed integration and retaining its
// calls for assertions at the test boundary.
const mcpProxy = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const raw = Buffer.concat(chunks).toString("utf8")
  if (request.method !== "POST") {
    const upstream = await fetch(fixture.mcpUrl, { method: request.method, headers: Object.fromEntries(Object.entries(request.headers).filter(([key]) => key !== "host")), body: raw || undefined })
    response.writeHead(upstream.status, Object.fromEntries(upstream.headers))
    if (upstream.body) Readable.fromWeb(upstream.body).pipe(response)
    else response.end()
    return
  }
  let rpc = {}
  try { rpc = JSON.parse(raw) } catch {}
  if (rpc.method === "tools/list") {
    response.writeHead(200, { "content-type": "application/json" })
    response.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result: { tools: [{
      name: "create_merge_request_note",
      description: "Create a review note on the addressed GitLab merge request.",
      inputSchema: { type: "object", properties: { project_id: { type: "integer" }, merge_request_iid: { type: "integer" }, body: { type: "string" } }, required: ["project_id", "merge_request_iid", "body"] },
    }] } }))
    return
  }
  if (rpc.method === "tools/call") {
    mcpCalls.push({ name: rpc.params?.name, arguments: rpc.params?.arguments ?? {} })
    response.writeHead(200, { "content-type": "application/json" })
    response.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result: { content: [{ type: "text", text: "comment created" }] } }))
    return
  }
  const upstream = await fetch(fixture.mcpUrl, { method: "POST", headers: Object.fromEntries(Object.entries(request.headers).filter(([key]) => key !== "host")), body: raw })
  response.writeHead(upstream.status, Object.fromEntries(upstream.headers))
  if (upstream.body) Readable.fromWeb(upstream.body).pipe(response)
  else response.end()
})
await new Promise((resolve) => mcpProxy.listen(0, "127.0.0.1", resolve))
const mcpUrl = `http://127.0.0.1:${mcpProxy.address().port}/mcp`
process.stdout.write(JSON.stringify({ modelUrl, mcpUrl }) + "\n")

let commandBuffer = ""
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => {
  commandBuffer += chunk
  while (commandBuffer.includes("\n")) {
    const newline = commandBuffer.indexOf("\n")
    const line = commandBuffer.slice(0, newline)
    commandBuffer = commandBuffer.slice(newline + 1)
    let command
    try { command = JSON.parse(line) } catch { continue }
    if (command.command === "capture") {
      process.stdout.write(JSON.stringify({ records, mcpCalls, closedResponses: records.filter((record) => record.responseClosed).length, heldModelRequests: heldResponses.filter((held) => !held.response.__achClosed).length }) + "\n")
    } else if (command.command === "hold") {
      holdModel = true
      process.stdout.write(JSON.stringify({ held: true }) + "\n")
    } else if (command.command === "scenario") {
      scenarioText = String(command.text ?? "pong")
      holdModel = false
      fixture.setModelHandler(() => ({ chunks: textChunks(scenarioText) }))
      process.stdout.write(JSON.stringify({ applied: true }) + "\n")
    } else if (command.command === "release") {
      holdModel = false
      for (const held of heldResponses.splice(0)) {
        const { response } = held
        if (response.__achClosed) continue
        response.end(`data: ${JSON.stringify(geminiReply(modelRequests++, held.body))}\n\n`)
      }
      process.stdout.write(JSON.stringify({ released: true }) + "\n")
    }
  }
})

process.stdin.resume() // keep the event loop alive; parent kills this process to stop it
