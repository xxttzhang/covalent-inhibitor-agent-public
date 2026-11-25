import { expect, test } from "bun:test"
import { Storage } from "../../src/storage/storage"
import { armLigandContinuation, pauseLigandContinuation, readLigandContinuation,
  tickLigandContinuation } from "../../src/session/ligand-continuation"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { Identifier } from "../../src/id/id"
import { tmpdir } from "../fixture/fixture"

const task = { entry: "science_covalent_ligand_workflow", workflow_id: "ligand-flow-" + "a".repeat(24),
  run_id: "registered-lo-test", request_id: "frozen-request" }

test("live terminal observation releases the old continuation for the next stage without a duplicate wake", async () => {
  const { registerLigandContinuation } = await import("../../src/session/ligand-continuation-runtime")
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const output = (run_id: string, job_state: string) => ({ structuredContent: {
    route: "ligand-first", workflow: { workflow_id: task.workflow_id }, result: { run_id, job_state },
  } })
  try {
    await armLigandContinuation(scope, task)
    await registerLigandContinuation(scope, task.entry, "receptor_preparation_status", output("foreign-run", "COMPLETED"))
    await registerLigandContinuation(scope, task.entry, "receptor_preparation_status", output(task.run_id, "RUNNING"))
    expect((await readLigandContinuation(scope))?.state).toBe("waiting")
    await registerLigandContinuation(scope, task.entry, "receptor_preparation_status", output(task.run_id, "COMPLETED"))
    expect((await readLigandContinuation(scope))?.state).toBe("observed")
    await tickLigandContinuation(scope, { active: async () => true, idle: () => true,
      observe: async () => { throw Error("Must not reobserve an acknowledged terminal task") },
      wake: async () => { throw Error("Must not duplicate a directly delivered terminal result") } })
    await armLigandContinuation(scope, { ...task, run_id: "next-stage-lo", request_id: "next-request" })
    expect((await readLigandContinuation(scope))?.run_id).toBe("next-stage-lo")
    expect((await readLigandContinuation(scope))?.state).toBe("waiting")
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("durable system notification repairs a partial write and never duplicates the message", async () => {
  const { queueLigandNotification } = await import("../../src/session/ligand-continuation-runtime")
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const parent = { role: "user" as const, id: Identifier.ascending("message"), sessionID: session.id,
      time: { created: Date.now() }, model: { providerID: "test", modelID: "test" }, agent: "build",
      effort: "normal" as const, tools: { bash: false } }
    await Session.updateMessage(parent)
    const saved = { ...task, state: "ready" as const, messageID: Identifier.ascending("message"),
      next: 0, expires: Date.now() + 60_000, observed: "COMPLETED" }
    // Crash after the message header, before its synthetic notification part.
    await Session.updateMessage({ ...parent, id: saved.messageID })
    expect(await queueLigandNotification(session.id, saved)).toBe(true)
    expect(await queueLigandNotification(session.id, saved)).toBe(true)
    const messages = await Session.messages({ sessionID: session.id })
    expect(messages).toHaveLength(2)
    const notification = messages.find(item => item.info.id === saved.messageID)!
    expect(notification.parts).toHaveLength(1)
    expect(notification.parts[0]).toMatchObject({ type: "text", synthetic: true })
    expect(notification.info).toMatchObject({ tools: { bash: false }, model: parent.model })
    await Session.remove(session.id)
  } })
})

test("successful native submission registers its exact task; status does not arm historical jobs", async () => {
  const { registerLigandContinuation } = await import("../../src/session/ligand-continuation-runtime")
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const output = { structuredContent: { route: "ligand-first", workflow: { workflow_id: task.workflow_id },
    covdock_execution: { run_id: task.run_id, request_id: task.request_id, job_state: "QUEUED" } } }
  try {
    await registerLigandContinuation(scope, task.entry, "status", output)
    expect(await readLigandContinuation(scope)).toBeUndefined()
    await registerLigandContinuation(scope, task.entry, "start_covdock", output)
    expect((await readLigandContinuation(scope))?.run_id).toBe(task.run_id)
    expect((await readLigandContinuation(scope))?.state).toBe("waiting")
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("persisted task waits, tolerates disconnect, and delivers a terminal notification once", async () => {
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const calls: string[] = []
  let state = "RUNNING"
  let offline = false
  const ports = {
    active: async () => true, idle: () => true,
    observe: async () => {
      if (offline) throw Error("connector unavailable")
      return { status: "PASS", spec: { ...task, route: "ligand-first" },
        job: { run_id: task.run_id, route: "ligand-first", state } }
    },
    wake: async (saved: { messageID: string }) => { calls.push(saved.messageID) },
  }
  try {
    const original = await armLigandContinuation(scope, task, 0)
    expect(await armLigandContinuation(scope, task, 1)).toEqual(original)
    await tickLigandContinuation(scope, ports, 0)
    expect(calls).toEqual([])
    offline = true
    await tickLigandContinuation(scope, ports, 60_000)
    expect((await readLigandContinuation(scope))?.state).toBe("waiting")
    offline = false
    state = "FAILED"
    await Promise.all([tickLigandContinuation(scope, ports, 120_000), tickLigandContinuation(scope, ports, 120_000)])
    expect(calls).toEqual([(await readLigandContinuation(scope))!.messageID])
    expect((await readLigandContinuation(scope))?.observed).toBe("FAILED")
    expect((await readLigandContinuation(scope))?.state).toBe("delivered")
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("foreign task cannot wake; pause survives another tick", async () => {
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  let calls = 0
  const ports = {
    active: async () => true, idle: () => true,
    observe: async () => ({ status: "PASS", spec: { ...task, run_id: "different-run", route: "ligand-first" },
      job: { run_id: task.run_id, route: "ligand-first", state: "COMPLETED" } }),
    wake: async () => { calls++ },
  }
  try {
    await armLigandContinuation(scope, task, 0)
    await tickLigandContinuation(scope, ports, 0)
    expect(calls).toBe(0)
    expect((await readLigandContinuation(scope))?.error).toBeDefined()
    await pauseLigandContinuation(scope)
    await tickLigandContinuation(scope, ports, 120_000)
    expect((await readLigandContinuation(scope))?.state).toBe("paused")
    expect(calls).toBe(0)
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("failed notification retries with the same durable message identity", async () => {
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const messages = new Set<string>()
  let attempts = 0
  const ports = {
    active: async () => true, idle: () => true,
    observe: async () => ({ status: "PASS", spec: { ...task, route: "ligand-first" },
      job: { run_id: task.run_id, route: "ligand-first", state: "COMPLETED" } }),
    wake: async (saved: { messageID: string }) => {
      messages.add(saved.messageID)
      if (++attempts === 1) throw Error("interrupted after notification persistence")
    },
  }
  try {
    await armLigandContinuation(scope, task, 0)
    await tickLigandContinuation(scope, ports, 0)
    expect((await readLigandContinuation(scope))?.state).toBe("ready")
    await tickLigandContinuation(scope, ports, 60_000)
    expect(messages.size).toBe(1)
    expect(attempts).toBe(2)
    expect((await readLigandContinuation(scope))?.state).toBe("delivered")
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("bound evidence completion wakes once and permits the next compute stage", async () => {
  const { registerLigandContinuation } = await import("../../src/session/ligand-continuation-runtime")
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const id = "hyp-" + "b".repeat(24)
  const output = (state: string) => ({ structuredContent: { route: "ligand-first",
    workflow: { workflow_id: task.workflow_id }, evidence_id: id, result: { evidence_id: id, state } } })
  let wakes = 0
  try {
    await registerLigandContinuation(scope, task.entry, "collect", output("RUNNING"))
    const initial = (await readLigandContinuation(scope))!
    expect(initial.kind).toBe("evidence")
    const tick = (evidence_id: string, state: string, now: number) => tickLigandContinuation(scope, {
      active: async () => true, idle: () => true,
      observe: async () => ({ evidence_id, state }), wake: async () => { wakes++ },
    }, now)
    await tick("hyp-" + "c".repeat(24), "EVIDENCE_READY", initial.next)
    expect(wakes).toBe(0)
    expect((await readLigandContinuation(scope))?.state).toBe("waiting")
    await tick(id, "RUNNING", initial.next + 60_000)
    expect(wakes).toBe(0)
    await tick(id, "EVIDENCE_READY", initial.next + 120_000)
    expect(wakes).toBe(1)
    await tick(id, "EVIDENCE_READY", initial.next + 180_000)
    expect(wakes).toBe(1)
    await armLigandContinuation(scope, task)
    expect((await readLigandContinuation(scope))?.run_id).toBe(task.run_id)
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})

test("direct evidence failure acknowledges the collection without duplicate wake", async () => {
  const { registerLigandContinuation } = await import("../../src/session/ligand-continuation-runtime")
  const scope = { project: "continuation-test", session: "ses_" + crypto.randomUUID() }
  const id = "hyp-" + "d".repeat(24)
  const output = (state: string) => ({ structuredContent: { route: "ligand-first",
    workflow: { workflow_id: task.workflow_id }, evidence_id: id, result: { evidence_id: id, state } } })
  try {
    await registerLigandContinuation(scope, task.entry, "collect", output("RUNNING"))
    await registerLigandContinuation(scope, task.entry, "read", output("FAILED"))
    expect((await readLigandContinuation(scope))?.state).toBe("observed")
    expect((await readLigandContinuation(scope))?.observed).toBe("FAILED")
  } finally { await Storage.remove(["ligand-continuations", scope.project, scope.session]) }
})
