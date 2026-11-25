import { expect, test } from "bun:test"
import { PermissionNext } from "../../src/permission/next"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { Bus } from "../../src/bus"
import { tmpdir } from "../fixture/fixture"

async function pending(sessionID: string, signal?: AbortSignal) {
  const asked = Promise.withResolvers<PermissionNext.Request>()
  const pattern = crypto.randomUUID()
  const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
    if (event.properties.patterns[0] === pattern) asked.resolve(event.properties)
  })
  const result = PermissionNext.ask({ sessionID, permission: "remote_compute", patterns: [pattern],
    always: [], metadata: { purpose: "local test only" }, ruleset: [], signal }).then(
    () => ({ granted: true, error: undefined }), (error: unknown) => ({ granted: false, error }))
  try { return { card: await asked.promise, result } } finally { unsubscribe() }
}

test("abort withdraws exactly one native card, never grants or rejects siblings, and ignores a late reply", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const controller = new AbortController()
    const first = await pending(session.id, controller.signal)
    const sibling = await pending(session.id)
    expect(first.card).not.toHaveProperty("signal")
    expect(await PermissionNext.list()).toHaveLength(2)
    controller.abort()
    const result = await first.result
    expect(result.granted).toBe(false)
    expect(result.error).toBe(controller.signal.reason)
    expect((await PermissionNext.list()).map((item) => item.id)).toEqual([sibling.card.id])
    await PermissionNext.reply({ requestID: first.card.id, reply: "once" })
    expect((await PermissionNext.list()).map((item) => item.id)).toEqual([sibling.card.id])
    await PermissionNext.reply({ requestID: sibling.card.id, reply: "once" })
    expect((await sibling.result).granted).toBe(true)
    expect(await PermissionNext.list()).toEqual([])
    // An interrupted request creates no standing resource approval.
    const fresh = await pending(session.id)
    expect(await PermissionNext.list()).toHaveLength(1)
    await PermissionNext.reply({ requestID: fresh.card.id, reply: "reject" })
    expect((await fresh.result).granted).toBe(false)
  } })
})

test("pre-aborted native request never creates a card, including with configured allow", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const controller = new AbortController()
    controller.abort()
    await expect(PermissionNext.ask({ sessionID: session.id, permission: "remote_compute", patterns: ["test"],
      always: [], metadata: {}, ruleset: [{ permission: "*", pattern: "*", action: "allow" }], signal: controller.signal })).rejects.toThrow()
    expect(await PermissionNext.list()).toEqual([])
  } })
})

test("abort from another project dismisses the original card only and emits its event in the original project", async () => {
  await using first = await tmpdir({ git: true })
  await using second = await tmpdir({ git: true })
  const controller = new AbortController()
  const captured = await Instance.provide({ directory: first.path, fn: async () => {
    const session = await Session.create({})
    const value = await pending(session.id, controller.signal)
    const replied = Promise.withResolvers<void>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Replied, (event) => {
      if (event.properties.requestID === value.card.id) replied.resolve()
    })
    return { ...value, replied, unsubscribe }
  } })
  await Instance.provide({ directory: second.path, fn: async () => {
    const session = await Session.create({})
    const other = await pending(session.id)
    controller.abort()
    expect((await captured.result).granted).toBe(false)
    await captured.replied.promise
    expect((await PermissionNext.list()).map((item) => item.id)).toEqual([other.card.id])
    await PermissionNext.reply({ requestID: other.card.id, reply: "once" })
    expect((await other.result).granted).toBe(true)
  } })
  await Instance.provide({ directory: first.path, fn: async () => {
    expect(await PermissionNext.list()).toEqual([])
    captured.unsubscribe()
  } })
})

test("aborting an already-settled request emits no second reply and leaves later cards intact", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const controller = new AbortController()
    const item = await pending(session.id, controller.signal)
    const replies: string[] = []
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Replied, (event) => replies.push(event.properties.requestID))
    try {
      await PermissionNext.reply({ requestID: item.card.id, reply: "once" })
      expect((await item.result).granted).toBe(true)
      const next = await pending(session.id)
      controller.abort()
      expect((await PermissionNext.list()).map((entry) => entry.id)).toEqual([next.card.id])
      expect(replies.filter((id) => id === item.card.id)).toHaveLength(1)
      await PermissionNext.reply({ requestID: next.card.id, reply: "reject" })
      await next.result
    } finally { unsubscribe() }
  } })
})
