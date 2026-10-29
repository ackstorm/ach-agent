// Fake OpenAI-compatible model + fake MCP server, both loopback-only.
// The model's behavior per request is driven by a caller-supplied handler
// (set via `setModelHandler`) so different scenarios can script different
// turns without spinning up a new server each time.
import { createServer } from "node:http"

function sse(res, chunks) {
  res.writeHead(200, { "content-type": "text/event-stream" })
  for (const c of chunks) res.write(`data: ${JSON.stringify(c)}\n\n`)
  res.write("data: [DONE]\n\n")
  res.end()
}

function toolCallChunks(calls, { usage } = {}) {
  return [
    {
      choices: [
        {
          index: 0,
          delta: {
            role: "assistant",
            tool_calls: calls.map((c, i) => ({ index: i, id: c.id, type: "function", function: { name: c.name, arguments: JSON.stringify(c.args ?? {}) } })),
          },
          finish_reason: null,
        },
      ],
    },
    { choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }], ...(usage ? { usage } : {}) },
  ]
}

function textChunks(text, { usage } = {}) {
  return [
    { choices: [{ index: 0, delta: { role: "assistant", content: text }, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }], ...(usage ? { usage } : {}) },
  ]
}

export { toolCallChunks, textChunks }

export async function startFakeFacades() {
  const requests = []
  const mcpRequests = []
  let modelHandler = defaultModelHandler

  function defaultModelHandler(req) {
    return { chunks: textChunks("ok") }
  }

  function setModelHandler(fn) {
    modelHandler = fn
  }

  let reqIndex = 0
  const modelServer = createServer((httpReq, res) => {
    const chunks = []
    httpReq.on("data", (c) => chunks.push(c))
    httpReq.on("end", () => {
      let body = {}
      try {
        body = JSON.parse(Buffer.concat(chunks).toString("utf8"))
      } catch {}
      const index = reqIndex++
      const kind = httpReq.headers["x-ach-probe-kind"] ?? "unclassified"
      const record = {
        index,
        kind,
        model: body.model ?? null,
        hasTools: Array.isArray(body.tools) && body.tools.length > 0,
        toolChoice: body.tool_choice ?? null,
        maxCompletionTokens: body.max_completion_tokens ?? null,
        maxTokens: body.max_tokens ?? null,
        messageCount: Array.isArray(body.messages) ? body.messages.length : 0,
        lastMessage: (Array.isArray(body.messages) && body.messages[body.messages.length - 1]) || undefined,
        toolNames: (body.tools ?? []).map((t) => t.function?.name).filter(Boolean),
        tools: (body.tools ?? []).map((t) => ({ name: t.function?.name, description: t.function?.description })),
      }
      // `record.closed` (request body) is NOT a cancellation signal: it
      // flips true as soon as the small JSON POST body finishes uploading,
      // long before any interrupt -- a cancel-detection oracle built on it
      // would false-PASS unconditionally. `record.responseClosed` (the
      // *response* stream we're holding open) is the real signal: it only
      // flips when the client actually tears down this still-open
      // connection (e.g. on interrupt), which Node's `res` "close" event
      // reports regardless of whether we ever called res.end() ourselves.
      record.closed = false
      record.responseClosed = false
      httpReq.on("close", () => (record.closed = true))
      res.on("close", () => (record.responseClosed = true))
      requests.push(record)
      const result = modelHandler(record, body) ?? { chunks: textChunks("ok") }
      if (result.hold) {
        // Leave the stream open (no [DONE], no res.end()) so a cancellation
        // test can interrupt mid-turn and observe `responseClosed` flip.
        res.writeHead(200, { "content-type": "text/event-stream" })
        for (const c of result.chunks ?? []) res.write(`data: ${JSON.stringify(c)}\n\n`)
        return
      }
      sse(res, result.chunks)
    })
  })
  await new Promise((resolve) => modelServer.listen(0, "127.0.0.1", resolve))
  const modelUrl = `http://127.0.0.1:${modelServer.address().port}/v1`

  // Minimal JSON-RPC 2.0 MCP server over HTTP (Streamable HTTP transport):
  // initialize, tools/list, tools/call (echoes `text`), empty
  // resources/prompts lists. Method-aware: only POST carries JSON-RPC;
  // GET (server-initiated SSE stream) and DELETE (session termination) are
  // handled by method, not force-parsed as a JSON-RPC body with id:undefined
  // (a real bug this fixture had -- both GET/DELETE bodies are empty, so
  // `JSON.parse("")` failing silently used to fall through to the `default`
  // case below and answer a non-request with a bogus response).
  const mcpServer = createServer((httpReq, res) => {
    if (httpReq.method === "GET") {
      // No server-initiated messages to push; 405 is spec-legal here.
      res.writeHead(405, { allow: "POST, DELETE" })
      return res.end()
    }
    if (httpReq.method === "DELETE") {
      mcpRequests.push({ method: "DELETE", params: null, authorization: httpReq.headers["authorization"] ?? null })
      res.writeHead(200)
      return res.end()
    }
    const chunks = []
    httpReq.on("data", (c) => chunks.push(c))
    httpReq.on("end", () => {
      let rpc
      try {
        rpc = JSON.parse(Buffer.concat(chunks).toString("utf8"))
      } catch {
        rpc = null
      }
      if (!rpc || typeof rpc.method !== "string") return res.writeHead(400).end() // not a JSON-RPC request; don't guess
      mcpRequests.push({ method: rpc.method, params: rpc.params ?? null, authorization: httpReq.headers["authorization"] ?? null })
      const isNotification = rpc.id === undefined // JSON-RPC notification: no response body, 202 per Streamable HTTP transport
      const reply = (result) => {
        if (isNotification) {
          res.writeHead(202)
          return res.end()
        }
        res.writeHead(200, { "content-type": "application/json" })
        res.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result }))
      }
      switch (rpc.method) {
        case "initialize":
          // Echo the client's requested protocolVersion back (standard MCP
          // negotiation) instead of a hardcoded, possibly-mismatched one.
          return reply({ protocolVersion: rpc.params?.protocolVersion ?? "2024-11-05", capabilities: { tools: {} }, serverInfo: { name: "fake-echo-mcp", version: "0.0.0" } })
        case "notifications/initialized":
          return reply(undefined)
        case "tools/list":
          return reply({ tools: [{ name: "echo", description: "echoes text", inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } }] })
        case "tools/call":
          return reply({ content: [{ type: "text", text: String(rpc.params?.arguments?.text ?? "") }] })
        case "resources/list":
          return reply({ resources: [] })
        case "prompts/list":
          return reply({ prompts: [] })
        default:
          res.writeHead(200, { "content-type": "application/json" })
          return res.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, result: {} }))
      }
    })
  })
  await new Promise((resolve) => mcpServer.listen(0, "127.0.0.1", resolve))
  const mcpUrl = `http://127.0.0.1:${mcpServer.address().port}/mcp`

  function waitForRequest(predicate, timeoutMs = 10_000) {
    const start = Date.now()
    return new Promise((resolve, reject) => {
      const check = () => {
        const found = requests.find(predicate)
        if (found) return resolve(found)
        if (Date.now() - start > timeoutMs) return reject(new Error("timed out waiting for a matching model request"))
        setTimeout(check, 50)
      }
      check()
    })
  }

  function closeListener(server, timeoutMs = 2000) {
    if (server.closePromise) return server.closePromise
    if (!server.listening) return Promise.resolve({ confirmed: true, listening: false })
    server.closePromise = new Promise((resolve) => {
      const timer = setTimeout(() => resolve({ confirmed: false, listening: server.listening, reason: `close timed out after ${timeoutMs}ms` }), timeoutMs)
      try {
        server.close((error) => {
          clearTimeout(timer)
          resolve({
            confirmed: !error && !server.listening,
            listening: server.listening,
            ...(error ? { error: String(error) } : {}),
          })
        })
        // Stop new accepts first, then terminate any HTTP/1 connections that
        // would otherwise keep the close callback pending. The callback is
        // the barrier: returning from server.close() alone proves nothing.
        server.closeAllConnections?.()
      } catch (error) {
        clearTimeout(timer)
        resolve({ confirmed: !server.listening, listening: server.listening, error: String(error) })
      }
    })
    return server.closePromise
  }

  async function close() {
    const [model, mcp] = await Promise.all([closeListener(modelServer), closeListener(mcpServer)])
    return { confirmed: model.confirmed && mcp.confirmed, model, mcp }
  }

  function listenerState() {
    return { model: modelServer.listening, mcp: mcpServer.listening }
  }

  return { modelUrl, mcpUrl, requests, mcpRequests, setModelHandler, waitForRequest, close, listenerState }
}
