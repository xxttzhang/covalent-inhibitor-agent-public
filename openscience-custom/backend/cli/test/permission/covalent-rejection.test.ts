import { expect, test } from "bun:test"
import { PermissionNext } from "../../src/permission/next"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"
import { SessionTraceStore } from "../../src/session/trace-store"
import { Bus } from "../../src/bus"
import { Global } from "../../src/global"
import path from "node:path"
import fs from "node:fs/promises"

test("exact covalent rejection preserves independent pending decisions and records only refused plans", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const ask = (id: string, digest: string) => PermissionNext.ask({
      id, sessionID: "session_scoped_refusal", permission: "remote_compute",
      patterns: [`covalent_tools_covalent_submit_job:${digest}`], always: [], ruleset: [],
      metadata: { route: "covalent-discovery", tool: "covalent_submit_job", execution_plan_sha256: digest },
    }).catch((error) => error)
    const first = ask("permission_scoped_first", "a".repeat(64))
    const duplicate = ask("permission_scoped_duplicate", "a".repeat(64))
    const other = ask("permission_scoped_other", "b".repeat(64))
    await PermissionNext.reply({ requestID: "permission_scoped_first", reply: "reject", message: "Budget too large" })
    expect(await first).toBeInstanceOf(PermissionNext.PlanRejectedError)
    expect((await duplicate).message).toContain("Budget too large")
    expect(await PermissionNext.list()).toHaveLength(1)
    expect((await PermissionNext.list())[0].id).toBe("permission_scoped_other")
    const trace = await SessionTraceStore.read("session_scoped_refusal")
    expect(trace.approvals.permission_scoped_first.reply).toBe("reject")
    expect(trace.approvals.permission_scoped_duplicate.reply).toBe("reject")
    expect(trace.approvals.permission_scoped_other.reply).toBeUndefined()
    // The independent call remains pending until an explicit separate reply.
    await PermissionNext.reply({ requestID: "permission_scoped_other", reply: "once" })
    expect(await other).toBeUndefined()
  } })
})

test("malformed plan metadata does not weaken generic rejection", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const first = PermissionNext.ask({ id: "permission_bad_plan", sessionID: "session_bad_plan",
      permission: "remote_compute", patterns: ["different-pattern"], always: [], ruleset: [],
      metadata: { route: "covalent-discovery", tool: "covalent_submit_job", execution_plan_sha256: "a".repeat(64) },
    }).catch((error) => error)
    const other = PermissionNext.ask({ id: "permission_other_generic", sessionID: "session_bad_plan",
      permission: "edit", patterns: ["file"], metadata: {}, always: [], ruleset: [],
    }).catch((error) => error)
    await PermissionNext.reply({ requestID: "permission_bad_plan", reply: "reject" })
    expect(await first).toBeInstanceOf(PermissionNext.RejectedError)
    expect(await other).toBeInstanceOf(PermissionNext.RejectedError)
    expect(await PermissionNext.list()).toHaveLength(0)
  } })
})

test("refused plan cannot repeat its approval card after reconnect in the same user turn", async () => {
  await using tmp = await tmpdir({ git: true })
  const session = `session_reconnected_${Bun.hash(tmp.path)}`
  const published = (id: string) => new Promise<void>((resolve) => {
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
      if (event.properties.id !== id) return
      unsubscribe()
      resolve()
    })
  })
  const request = (id: string, turn: string) => ({
    id, sessionID: session, permission: "remote_compute",
    patterns: [`covalent_tools_covalent_submit_job:${"c".repeat(64)}`], always: [], ruleset: [],
    metadata: { route: "covalent-discovery", tool: "covalent_submit_job",
      execution_plan_sha256: "c".repeat(64), user_message_id: turn },
  })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const ready = published("permission_turn_first")
    const first = PermissionNext.ask(request("permission_turn_first", "user_turn_one")).catch((error) => error)
    await ready
    await PermissionNext.reply({ requestID: "permission_turn_first", reply: "reject" })
    expect(await first).toBeInstanceOf(PermissionNext.PlanRejectedError)
    await Instance.dispose()
  } })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    await expect(PermissionNext.ask(request("permission_turn_repeat", "user_turn_one")))
      .rejects.toBeInstanceOf(PermissionNext.PlanRejectedError)
    expect(await PermissionNext.list()).toHaveLength(0)
    expect((await SessionTraceStore.read(session)).approvals.permission_turn_repeat).toBeUndefined()
    const ready = published("permission_turn_new")
    const reconsider = PermissionNext.ask(request("permission_turn_new", "user_turn_two"))
    await ready
    expect((await PermissionNext.list())[0].id).toBe("permission_turn_new")
    await PermissionNext.reply({ requestID: "permission_turn_new", reply: "once" })
    await reconsider
    expect((await SessionTraceStore.read(session)).approvals.permission_turn_first.reply).toBe("reject")
  } })
})

test("damaged approval audit blocks a new card without discarding evidence", async () => {
  await using tmp = await tmpdir({ git: true })
  const session = `session_damaged_${Bun.hash(tmp.path)}`
  const file = path.join(Global.Path.data, "trace", `${encodeURIComponent(session)}.json`)
  await Instance.provide({ directory: tmp.path, fn: async () => {
    for (const content of ["{broken", "", '{"approvals":false}', 'null']) {
      await Bun.write(file, content)
      await expect(PermissionNext.ask({
        sessionID: session, permission: "remote_compute",
        patterns: [`covalent_tools_covalent_submit_job:${"d".repeat(64)}`], always: [], ruleset: [],
        metadata: { route: "covalent-discovery", tool: "covalent_submit_job",
          execution_plan_sha256: "d".repeat(64), user_message_id: "user_damaged_audit" },
      })).rejects.toThrow()
      expect(await PermissionNext.list()).toHaveLength(0)
      expect(await Bun.file(file).text()).toBe(content)
    }
    await fs.unlink(file)
    expect((await SessionTraceStore.readForApproval(session)).approvals).toEqual({})
  } })
})

test("a decision that cannot be persisted rejects its waiting operation", async () => {
  await using tmp = await tmpdir({ git: true })
  const session = `session_write_failure_${Bun.hash(tmp.path)}`
  const file = path.join(Global.Path.data, "trace", `${encodeURIComponent(session)}.json`)
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const ready = new Promise<void>((resolve) => {
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID !== session) return
        unsubscribe()
        resolve()
      })
    })
    const operation = PermissionNext.ask({
      id: "permission_write_failure", sessionID: session, permission: "remote_compute",
      patterns: [`covalent_tools_covalent_submit_job:${"e".repeat(64)}`], always: [], ruleset: [],
      metadata: { route: "covalent-discovery", tool: "covalent_submit_job",
        execution_plan_sha256: "e".repeat(64), user_message_id: "user_write_failure" },
    }).catch((error) => error)
    await ready
    // JsonStore serializes this update after approvalAsked: the request has
    // reached disk, but its schema is damaged before the reply is persisted.
    const { JsonStore } = await import("../../src/util/jsonstore")
    await JsonStore.update(file, () => ({ approvals: false }))
    await expect(PermissionNext.reply({ requestID: "permission_write_failure", reply: "once" })).rejects.toThrow()
    expect(await operation).toBeInstanceOf(Error)
    expect(await PermissionNext.list()).toHaveLength(0)
    expect(await Bun.file(file).json()).toEqual({ approvals: false })
    await fs.unlink(file)
  } })
})
