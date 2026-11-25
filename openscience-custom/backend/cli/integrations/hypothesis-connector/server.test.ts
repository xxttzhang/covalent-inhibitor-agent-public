import { expect, test } from "bun:test"
import { authorized, keepalive, start } from "./server"

test("long MCP streams retain events, emit keepalives, and close after delivery", async () => {
  const state = { closed: 0 }
  const source = new ReadableStream<Uint8Array>({
    async start(controller) {
      await Bun.sleep(40)
      controller.enqueue(new TextEncoder().encode('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'))
      controller.close()
    },
  })
  const response = await keepalive(new Response(source, { headers: { "content-type": "text/event-stream" } }),
    async () => { state.closed++ }, 5)
  expect(state.closed).toBe(0)
  const body = await response.text()
  expect(body).toContain(": keep-alive\n\n")
  expect(body).toContain('data: {"jsonrpc":"2.0","id":1,"result":{}}')
  expect(state.closed).toBe(1)
})

test("non-stream acknowledgements keep their status and close immediately", async () => {
  const state = { closed: 0 }
  const response = await keepalive(new Response(null, { status: 202 }), async () => { state.closed++ })
  expect(response.status).toBe(202)
  expect(await response.text()).toBe("")
  expect(state.closed).toBe(1)
})

test("negotiated clients receive SSE while legacy administrative calls retain JSON", async () => {
  const server = start("test-secret-only", 0)
  const url = `http://127.0.0.1:${server.port}/covalent-discovery`
  const headers = { Authorization: "Bearer test-secret-only", "Content-Type": "application/json",
    Accept: "application/json, text/event-stream" }
  const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "unsupported_test_method" })
  try {
    const legacy = await fetch(url, { method: "POST", headers, body })
    expect(legacy.headers.get("content-type")).toContain("application/json")
    expect((await legacy.json()).error.code).toBe(-32601)
    const negotiated = await fetch(url, { method: "POST", headers: { ...headers,
      "mcp-protocol-version": "2025-03-26" }, body })
    expect(negotiated.headers.get("content-type")).toContain("text/event-stream")
    expect(await negotiated.text()).toContain('"code":-32601')
  } finally { server.stop(true) }
})

test("connector rejects unauthenticated requests and browser origins", async () => {
  const server = start("test-secret-only", 0)
  const url = `http://127.0.0.1:${server.port}/health`
  try {
    expect((await fetch(url)).status).toBe(401)
    expect((await fetch(url, { headers: { Authorization: "Bearer test-secret-only", Origin: "https://other.test" } })).status).toBe(401)
    expect((await fetch(url, { headers: { Authorization: "Bearer wrong" } })).status).toBe(401)
    expect((await fetch(url, { headers: { Authorization: "Bearer test-secret-only" } })).status).toBe(200)
    expect(authorized(new Request(url, { headers: { host: "other.test", Authorization: "Bearer test-secret-only" } }), "test-secret-only", server.port!)).toBe(false)
  } finally { server.stop(true) }
})
