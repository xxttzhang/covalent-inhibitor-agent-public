import { describe, expect, test } from "bun:test"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import { authorize, querySchema, start } from "./server"

describe("Inspire database connector boundary", () => {
  test("requires a target or inhibitor and rejects arbitrary paths", () => {
    expect(querySchema.safeParse({ target: "EGFR", site: "C797" }).success).toBe(true)
    expect(querySchema.safeParse({ site: "C797" }).success).toBe(false)
    expect(querySchema.safeParse({ target: "EGFR", path: "/etc/passwd" }).success).toBe(false)
    expect(querySchema.safeParse({ target: "EGFR", limit: 10000 }).success).toBe(false)
  })
  test("requires bearer token, exact loopback host and no browser origin", () => {
    const request = (headers: Record<string, string>) => new Request("http://127.0.0.1:4097/mcp", { headers })
    expect(authorize(request({ host: "127.0.0.1:4097", authorization: "Bearer test" }), "test", 4097)).toBe(true)
    expect(authorize(request({ host: "127.0.0.1:4097" }), "test", 4097)).toBe(false)
    expect(authorize(request({ host: "evil.test:4097", authorization: "Bearer test" }), "test", 4097)).toBe(false)
    expect(authorize(request({ host: "127.0.0.1:4097", authorization: "Bearer test", origin: "https://evil.test" }), "test", 4097)).toBe(false)
  })
  test("SDK client discovers tools/resources/prompt; invalid calls do not access Inspire", async () => {
    const server = start("test-secret", 0)
    const port = server.port
    const client = new Client({ name: "integration-test", version: "1.0.0" })
    try {
      expect((await fetch(`http://127.0.0.1:${port}/mcp`, { method: "POST" })).status).toBe(401)
      await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), {
        requestInit: { headers: { Authorization: "Bearer test-secret" } },
      }))
      const tools = await client.listTools()
      expect(tools.tools.map((t) => t.name)).toEqual(["covindb_catalog", "covindb_query"])
      expect(tools.tools.every((t) => t.annotations?.readOnlyHint)).toBe(true)
      expect((await client.listResources()).resources).toHaveLength(1)
      expect((await client.listPrompts()).prompts).toHaveLength(1)
      const invalid = await client.callTool({ name: "covindb_query", arguments: { site: "C797" } })
      expect(invalid.isError).toBe(true)
    } finally {
      await client.close()
      server.stop(true)
    }
  })
})
