import { expect, test } from "bun:test"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js"
import { createOpenScienceClient } from "@synsci/sdk"
import type { ToolContext } from "@synsci/plugin/tool"
import { capability, capabilities, contract, invoke, server } from "./mcp"
import plugin from "./plugin"
import { Instance } from "../../../src/project/instance"
import { Server } from "../../../src/server/server"
import { Session } from "../../../src/session"
import { Identifier } from "../../../src/id/id"
import { tmpdir } from "../../../test/fixture/fixture"
import { type WorkflowReference } from "./workflow-execution"

function context(directory: string, sessionID = "not-a-session", messageID = "not-a-message"): ToolContext {
  return { directory, worktree: directory, sessionID, messageID, agent: "research",
    abort: new AbortController().signal, metadata: () => {},
    ask: async () => { throw new Error("Read-only MCP verification must not request compute") } }
}

test("workflow MCP lifecycle hides task identity and cannot create, replace or self-approve a task", async () => {
  await using directory = await tmpdir({ git: true })
  const host = createOpenScienceClient({ baseUrl: "http://openscience.internal", fetch: Server.internalFetch() })
  const ref: WorkflowReference = { schema: "library-workflow-execution-reference-v1", workflow_id: "library-flow-" + "a".repeat(24),
    revision: 3, operation_id: "b".repeat(32), intent_sha256: "c".repeat(64), plan_sha256: "d".repeat(64) }
  const mcp = server(host, context(directory.path), ref)
  const client = new Client({ name: "workflow-contract-test", version: "1" })
  const [caller, callee] = InMemoryTransport.createLinkedPair()
  try {
    await mcp.connect(callee)
    await client.connect(caller)
    const discovery = await client.listTools()
    const schema = discovery.tools.find(tool => tool.name === contract.name)!.inputSchema
    expect(schema.properties).not.toHaveProperty("task_id")
    expect(schema.properties).not.toHaveProperty("request")
    for (const args of [{ action: "create" }, { action: "list" }, { action: "status", task_id: "screen-" + "e".repeat(24) },
      { action: "approve", human_approved: true }, { action: "status", workflow: ref }]) {
      const rejected = await client.callTool({ name: contract.name, arguments: args })
      expect(rejected.isError).toBe(true)
    }
  } finally { await client.close(); await mcp.close() }
})

test("real MCP discovery exposes one executor and its truthful capabilities, without historical defaults", async () => {
  await using directory = await tmpdir({ git: true })
  const host = createOpenScienceClient({ baseUrl: "http://openscience.internal", fetch: Server.internalFetch() })
  const native = context(directory.path)
  const mcp = server(host, native)
  const client = new Client({ name: "contract-test", version: "1" })
  const [caller, callee] = InMemoryTransport.createLinkedPair()
  try {
    await mcp.connect(callee)
    await client.connect(caller)
    const discovered = await client.listTools()
    expect(discovered.tools.map(tool => tool.name).sort()).toEqual([capability.name, contract.name].sort())
    expect(discovered.tools.find(tool => tool.name === contract.name)?.inputSchema.additionalProperties).toBe(false)
    const result = await client.callTool({ name: capability.name, arguments: {} })
    expect(result.isError).not.toBe(true)
    expect(result.structuredContent).toEqual(capabilities())
    expect(result.structuredContent).toMatchObject({ runtime_verified: false, compute_submitted: false })
    const hooks = await plugin({ client: host } as Parameters<typeof plugin>[0])
    expect(Object.keys(hooks.tool).sort()).toEqual(discovered.tools.map(tool => tool.name).sort())
    expect(JSON.parse(await hooks.tool.inspire_screening_capabilities.execute({}, native))).toEqual(capabilities())

    // Invalid contracts must fail before opening stores or allocating resources.
    for (const args of [{ action: "status" }, { action: "list", human_approved: true },
      { action: "start_md" }, { action: "create", sessionID: "forged" }]) {
      const rejected = await client.callTool({ name: contract.name, arguments: args })
      expect(rejected.isError).toBe(true)
    }
  } finally {
    await client.close()
    await mcp.close()
  }
})

test("MCP create binds the persisted user message before execution, never the model's replacement", async () => {
  await using directory = await tmpdir({ git: true })
  await Instance.provide({ directory: directory.path, fn: async () => {
    const session = await Session.create({})
    const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
      agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
    await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: user.id,
      type: "text", text: "x".repeat(10001) })
    const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
      parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
      path: { cwd: directory.path, root: directory.path }, time: { created: Date.now() },
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
    const host = createOpenScienceClient({ baseUrl: "http://openscience.internal", fetch: Server.internalFetch() })
    const native = context(directory.path, session.id, assistant.id)
    for (const args of [{ action: "create" }, { action: "create", request: "model-authored replacement" }]) {
      await expect(invoke(host, native, contract.name, args)).rejects.toThrow("clarify rather than truncate")
    }
    const abort = new AbortController()
    abort.abort(new Error("cancelled before transport"))
    await expect(invoke(host, { ...native, abort: abort.signal }, capability.name, {})).rejects.toThrow("cancelled before transport")
  } })
})
