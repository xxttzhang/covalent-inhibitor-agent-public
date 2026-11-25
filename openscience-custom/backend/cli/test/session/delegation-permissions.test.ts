import { expect, test } from "bun:test"
import { tmpdir } from "../fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { PermissionNext } from "../../src/permission/next"
import { askToolPermission, checkToolPermission, sessionPermissionScopes, toolPermissions } from "../../src/session/tool-permissions"
import { Bus } from "../../src/bus"
import { TodoWriteTool } from "../../src/tool/todo"
import { Todo } from "../../src/session/todo"
import { TaskTool } from "../../src/tool/task"
import type { Tool } from "../../src/tool/tool"

const allow: PermissionNext.Ruleset = [{ permission: "*", pattern: "*", action: "allow" }]
const agent = { permission: allow }

function context(id: string): Tool.Context {
  return { sessionID: id, messageID: "msg_delegation_permissions", callID: "call_delegation_permissions",
    agent: "research", messages: [], abort: new AbortController().signal, metadata() {},
    ask: async request => askToolPermission({ ...request, sessionID: id, ruleset: await toolPermissions(agent, id) }) }
}

// Real pending permission event, not a stubbed authorization response.
async function permissionEvent(run: () => Promise<unknown>) {
  const event = Promise.withResolvers<PermissionNext.Request>()
  const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, e => event.resolve(e.properties))
  const outcome = run().then(value => ({ value, error: undefined }), (error: unknown) => ({ value: undefined, error }))
  const request = await Promise.race([event.promise, outcome.then(r => { throw r.error ?? Error("Permission was bypassed") })])
  unsubscribe()
  return { request, outcome }
}

test("ancestor scopes keep exceptions, child denials, and live revocation across grandchildren", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parent = await Session.create({ permission: [
      { permission: "*", pattern: "*", action: "deny" },
      { permission: "mcp", pattern: "science_read", action: "allow" },
    ] })
    const child = await Session.create({ parentID: parent.id, permission: allow })
    const leaf = await Session.create({ parentID: child.id, permission: allow })
    expect(await sessionPermissionScopes(leaf.id)).toHaveLength(3)
    await checkToolPermission("science_read", agent, leaf.id, true)
    await expect(checkToolPermission("science_write", agent, leaf.id, true)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await Session.update(child.id, draft => { draft.permission = [{ permission: "science_read", pattern: "*", action: "deny" }] })
    await expect(checkToolPermission("science_read", agent, leaf.id, true)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await Session.update(child.id, draft => { draft.permission = allow })
    await Session.update(parent.id, draft => { draft.permission = allow })
    await checkToolPermission("science_write", agent, leaf.id, true)
    await Session.update(parent.id, draft => { draft.permission = [{ permission: "mcp", pattern: "science_write", action: "deny" }] })
    await expect(checkToolPermission("science_write", agent, leaf.id, true)).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await Session.update(parent.id, draft => { draft.parentID = leaf.id })
    await expect(sessionPermissionScopes(leaf.id)).rejects.toThrow("Cyclic")
  } })
})

test("argument restrictions survive a permissive child and a conversation grant", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parent = await Session.create({ permission: [{ permission: "read", pattern: "private/*", action: "deny" }] })
    const child = await Session.create({ parentID: parent.id, permission: allow })
    const ask = (name: string) => askToolPermission({ sessionID: child.id, permission: "read", patterns: [name], always: [name], metadata: {}, ruleset: allow })
    await ask("public/report.json")
    await expect(ask("private/source.sdf")).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    await Session.update(parent.id, draft => { draft.permission = [{ permission: "read", pattern: "private/*", action: "ask" }] })
    const pending = await permissionEvent(() => ask("private/source.sdf"))
    await PermissionNext.reply({ requestID: pending.request.id, reply: "session" })
    expect((await pending.outcome).error).toBeUndefined()
    await ask("private/source.sdf") // Explicit conversation grant answers the inherited ask.
    await Session.update(parent.id, draft => { draft.permission = [{ permission: "read", pattern: "private/*", action: "deny" }] })
    await expect(ask("private/source.sdf")).rejects.toBeInstanceOf(PermissionNext.DeniedError)
  } })
})

test("revocation during native approval wait prevents the real tool side effect", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parent = await Session.create({ permission: [{ permission: "todowrite", pattern: "*", action: "ask" }] })
    const child = await Session.create({ parentID: parent.id, permission: allow })
    const tool = await TodoWriteTool.init()
    const pending = await permissionEvent(() => tool.execute({ todos: [
      { id: "1", content: "Must never be persisted", status: "pending", priority: "low" },
    ] }, context(child.id)))
    expect(await Todo.get(child.id)).toEqual([])
    await Session.update(parent.id, draft => { draft.permission = [{ permission: "todowrite", pattern: "*", action: "deny" }] })
    await PermissionNext.reply({ requestID: pending.request.id, reply: "once" })
    expect((await pending.outcome).error).toBeInstanceOf(PermissionNext.DeniedError)
    expect(await Todo.get(child.id)).toEqual([])
  } })
})

test("explicit subtask cannot bypass a scoped parent denial or create a child", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ permission: [{ permission: "task", pattern: "execute", action: "deny" }] })
    const tool = await TaskTool.init()
    await expect(tool.execute({ description: "Denied task", prompt: "Do not run", subagent_type: "execute" },
      { ...context(session.id), extra: { bypassAgentCheck: true } })).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    expect(await Session.children(session.id)).toEqual([])
  } })
})

test("remote compute denial cannot be replaced by a child allow or an approval card", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parent = await Session.create({ permission: [{ permission: "remote_compute", pattern: "*", action: "deny" }] })
    const child = await Session.create({ parentID: parent.id, permission: allow })
    await expect(askToolPermission({ sessionID: child.id, permission: "remote_compute",
      patterns: ["a".repeat(64)], always: [], metadata: {}, ruleset: allow })).rejects.toBeInstanceOf(PermissionNext.DeniedError)
    expect((await PermissionNext.list()).filter(r => r.sessionID === child.id)).toEqual([])
  } })
})
