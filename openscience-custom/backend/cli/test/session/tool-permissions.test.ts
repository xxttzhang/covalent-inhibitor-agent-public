import { expect, test } from "bun:test"
import path from "node:path"
import { tmpdir } from "../fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { PermissionNext } from "../../src/permission/next"
import { toolPermissions, updateToolPermissions, checkToolPermission } from "../../src/session/tool-permissions"
import { ToolRegistry } from "../../src/tool/registry"
import { listStudies } from "../../integrations/discovery-platform/study-store"
import { Bus } from "../../src/bus"
import type { Tool } from "../../src/tool/tool"
import { BatchTool } from "../../src/tool/batch"
import { Todo } from "../../src/session/todo"

const agent = { permission: [{ permission: "*", pattern: "*", action: "allow" as const }] }

test("batch rejects a denied member before dispatch and permits an allowed sibling", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ permission: [
      { permission: "todowrite", pattern: "*", action: "deny" },
      { permission: "todoread", pattern: "*", action: "allow" },
    ] })
    const requests: string[] = []
    const context: Tool.Context = { sessionID: session.id, messageID: "msg_batch_permission", agent: "research",
      abort: new AbortController().signal, messages: [], metadata() {},
      ask: async request => {
        requests.push(request.permission)
        await PermissionNext.ask({ ...request, sessionID: session.id, ruleset: await toolPermissions(agent, session.id) })
      } }
    const tool = await BatchTool.init()
    const result = await tool.execute({ tool_calls: [
      { tool: "todowrite", parameters: { todos: [{ id: "1", content: "Must not be written", status: "pending", priority: "low" }] } },
      { tool: "todoread", parameters: {} },
    ] }, context)
    expect(result.metadata.successful).toBe(1)
    expect(result.metadata.failed).toBe(1)
    expect(requests).toEqual(["todoread"])
    expect(await Todo.get(session.id)).toEqual([])
  } })
}, 30000)

test("legacy toggles retain remote denials and dispatch sees later session changes", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ permission: [
      { permission: "remote_compute", pattern: "*", action: "deny" },
      { permission: "discovery_study", pattern: "*", action: "deny" },
      { permission: "read", pattern: "private/*", action: "deny" },
    ] })
    await updateToolPermissions(session.id, { todowrite: false })
    const rules = await toolPermissions(agent, session.id)
    expect(PermissionNext.evaluate("remote_compute", "plan", rules).action).toBe("deny")
    expect(PermissionNext.evaluate("read", "private/input.sdf", rules).action).toBe("deny")
    expect(PermissionNext.evaluate("todowrite", "*", rules).action).toBe("deny")
    await expect(checkToolPermission("discovery_study", agent, session.id)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await updateToolPermissions(session.id, { discovery_study: true })
    await checkToolPermission("discovery_study", agent, session.id)
    await updateToolPermissions(session.id, { discovery_study: false })
    await expect(checkToolPermission("discovery_study", agent, session.id)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    expect(PermissionNext.evaluate("remote_compute", "plan", await toolPermissions(agent, session.id)).action).toBe("deny")
    const restricted = { permission: [{ permission: "*", pattern: "*", action: "deny" as const }] }
    await Session.update(session.id, draft => { draft.permission = [{ permission: "mcp", pattern: "science_read", action: "allow" }] })
    await checkToolPermission("science_read", restricted, session.id, true)
    await expect(checkToolPermission("science_write", restricted, session.id, true)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await updateToolPermissions(session.id, { science_read: false })
    await expect(checkToolPermission("science_read", restricted, session.id, true)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
  } })
})

test("actual discovery plugin checks permission before creating records and after revocation", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ permission: [{ permission: "discovery_study", pattern: "*", action: "deny" }] })
    const definition = await ToolRegistry.resolve("discovery_study", { providerID: "test", modelID: "test" })
    expect(definition).toBeDefined()
    const tool = definition!
    const context: Tool.Context = { sessionID: session.id, messageID: "msg_permission_fixture", agent: "research",
      callID: "permission-fixture", abort: new AbortController().signal, messages: [], metadata() {},
      ask: async request => { await PermissionNext.ask({ ...request, sessionID: session.id,
        ruleset: await toolPermissions(agent, session.id) }) } }
    const input = { action: "create", request: { title: "Permission integration fixture",
      target: { id: "fixture", gene: "FIXTURE", taxon: 9606, uniprot: "FIXTURE",
        site: { chain: "A", residue: "CYS", number: 1, atom: "SG", numbering: "canonical" }, receptor: { reference: "synthetic-test-only" } },
      hypothesis: { state: "pending", rationale: "Software test, no scientific evidence", evidence: [] },
      routes: { "de-novo": { selected: true, mode: "new", inputs: {} } } } }
    await expect(tool.execute(input, context)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    expect(await listStudies(tmp.path, Instance.project.id)).toHaveLength(0)
    expect(await Bun.file(path.join(tmp.path, ".discovery/studies")).exists()).toBe(false)
    await updateToolPermissions(session.id, { discovery_study: true })
    const result = JSON.parse((await tool.execute(input, context)).output)
    expect(result.study_id).toMatch(/^study-/)
    expect(await listStudies(tmp.path, Instance.project.id)).toHaveLength(1)
    await updateToolPermissions(session.id, { discovery_study: false })
    await expect(tool.execute(input, context)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    expect(await listStudies(tmp.path, Instance.project.id)).toHaveLength(1)
    // An explicit ask rule must reach native permission UI, not execute first.
    await Session.update(session.id, draft => { draft.permission = [{ permission: "discovery_study", pattern: "*", action: "ask" }] })
    const pending = Promise.withResolvers<PermissionNext.Request>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => pending.resolve(event.properties))
    const execution = tool.execute({ action: "list" }, context).then(value => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }))
    try {
      const request = await Promise.race([pending.promise, execution.then(result => { throw result.error ?? Error("Permission was bypassed") })])
      expect(request.permission).toBe("discovery_study")
      expect(await listStudies(tmp.path, Instance.project.id)).toHaveLength(1)
      await PermissionNext.reply({ requestID: request.id, reply: "reject" })
      expect((await execution).error).toBeDefined()
    } finally { unsubscribe() }
  } })
}, 30000)
