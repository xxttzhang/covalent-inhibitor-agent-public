import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { Server } from "@modelcontextprotocol/sdk/server/index.js"
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"
import { ListToolsRequestSchema, CallToolRequestSchema, ListResourcesRequestSchema, ListPromptsRequestSchema } from "@modelcontextprotocol/sdk/types.js"
import { timingSafeEqual } from "node:crypto"
import path from "node:path"

const root = path.resolve(import.meta.dir, "../../../../..")
export function keepalive(response: Response, close: () => Promise<void>, interval = 30000) {
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream"))
    return close().then(() => response)
  const reader = response.body.getReader()
  const state = { closed: false, timer: undefined as ReturnType<typeof setInterval> | undefined }
  const cleanup = async () => {
    if (state.closed) return
    state.closed = true
    clearInterval(state.timer)
    await close()
  }
  return new Response(new ReadableStream<Uint8Array>({
    async start(controller) {
      state.timer = setInterval(() => controller.enqueue(new TextEncoder().encode(": keep-alive\n\n")), interval)
      try {
        for (;;) {
          const chunk = await reader.read()
          if (chunk.done) break
          controller.enqueue(chunk.value)
        }
        if (!state.closed) controller.close()
      } catch (error) {
        if (!state.closed) controller.error(error)
      } finally { await cleanup() }
    },
    async cancel() {
      await cleanup()
      await reader.cancel()
    },
  }), { status: response.status, statusText: response.statusText, headers: response.headers })
}

export function authorized(request: Request, secret: string, port: number) {
  if (request.headers.get("host") !== `127.0.0.1:${port}` || request.headers.has("origin")) return false
  const value = Buffer.from(request.headers.get("authorization") ?? "")
  const expected = Buffer.from(`Bearer ${secret}`)
  return value.length === expected.length && timingSafeEqual(value, expected)
}

export function start(secret: string, port = 4101,
  binding?: { command: string; args: string[]; env: Record<string, string> }) {
  const listener = Bun.serve({ hostname: "127.0.0.1", port, idleTimeout: 180, maxRequestBodySize: 1_000_000,
    async fetch(request) {
      const url = new URL(request.url)
      if (!authorized(request, secret, listener.port!)) return new Response("Unauthorized", { status: 401 })
      if (url.pathname === "/health") return Response.json({ service: "hypothesis-route-connector", healthy: true })
      const route = url.pathname.slice(1)
      if (!["target-hypothesis", "covalent-discovery"].includes(route)) return new Response("Not found", { status: 404 })
      if (binding && route !== "covalent-discovery") return new Response("Not found", { status: 404 })
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405 })
      // Shared scientific planning replays immutable molecular evidence before
      // returning. Its bounded RPC deadline owns the wait, not HTTP idle time.
      if (route === "covalent-discovery") listener.timeout(request, 0)
      const server = new Server({ name: "hypothesis-route-connector", version: "1.0.0" },
        { capabilities: { tools: {}, resources: {}, prompts: {} } })
      const client = new Client({ name: "hypothesis-route-connector", version: "1.0.0" })
      // Connect once for this request. No retry of uncertain tools/call outcomes.
      const pending = () => client.connect(new StdioClientTransport({
        command: binding?.command ?? path.join(root, ".cache/refinement-validation-python310/bin/python"),
        args: binding?.args ?? [path.join(root, "ops/hypothesis_remote_mcp.py"), route],
        env: binding?.env, stderr: "inherit",
      }))
      const connected = { promise: undefined as Promise<void> | undefined }
      const ready = () => connected.promise ??= pending()
      server.setRequestHandler(ListToolsRequestSchema, async () => { await ready(); return client.listTools() })
      server.setRequestHandler(CallToolRequestSchema, async (message) => {
        await ready()
        return client.callTool(message.params, undefined, { timeout: route === "covalent-discovery" ? 600000 : 120000 })
      })
      server.setRequestHandler(ListResourcesRequestSchema, async () => ({ resources: [] }))
      server.setRequestHandler(ListPromptsRequestSchema, async () => ({ prompts: [] }))
      // Negotiated SDK clients consume SSE. Keep the existing JSON response
      // contract for one-shot administrative clients without protocol headers.
      const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined,
        enableJsonResponse: !request.headers.has("mcp-protocol-version") })
      await server.connect(transport)
      const close = async () => { await client.close(); await server.close() }
      try { return await keepalive(await transport.handleRequest(request), close) }
      catch (error) { await close(); throw error }
    },
  })
  return listener
}

if (import.meta.main) {
  const filename = process.env.HYPOTHESIS_CONNECTOR_KEY_FILE
  if (!filename) throw new Error("Connector key path required")
  const stat = await import("node:fs/promises").then(fs => fs.lstat(filename))
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077)) throw new Error("Connector key must be owner-only")
  const secret = (await Bun.file(filename).text()).trim()
  if (secret.length < 32) throw new Error("Connector key too short")
  start(secret)
}
