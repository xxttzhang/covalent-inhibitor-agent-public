import { expect, test } from "bun:test"
import { Storage } from "../../src/storage/storage"
import { acknowledgeDeNovoContinuation, armDeNovoContinuation, pauseDeNovoContinuation, readDeNovoContinuation, resumeDeNovoContinuation,
  tickDeNovoContinuation } from "../../src/session/denovo-continuation"

const task = { study: "study-test", stage: "reasoning" as const, task: "ses-child", request: "study-test" }

test("durable terminal notification retains its identity across interrupted delivery and concurrent ticks", async () => {
  const scope = { project: "denovo-test", session: "ses_" + crypto.randomUUID() }
  const delivered = new Set<string>()
  let attempts = 0
  const ports = { active: async () => true, idle: () => true,
    observe: async () => ({ ...task, state: "COMPLETED" }),
    wake: async (saved: { messageID: string }) => {
      delivered.add(saved.messageID)
      if (++attempts === 1) throw Error("Interrupted after durable notification")
    } }
  try {
    await armDeNovoContinuation(scope, task, 0)
    await tickDeNovoContinuation(scope, ports, 0)
    expect((await readDeNovoContinuation(scope))?.state).toBe("ready")
    await Promise.all([tickDeNovoContinuation(scope, ports, 60_000), tickDeNovoContinuation(scope, ports, 60_000)])
    expect(delivered.size).toBe(1)
    expect(attempts).toBe(2)
    expect((await readDeNovoContinuation(scope))?.state).toBe("delivered")
  } finally { await Storage.remove(["denovo-continuations", scope.project, scope.session]) }
})

test("stop before registration survives startup; a new user turn resumes only the original task", async () => {
  const scope = { project: "denovo-test", session: "ses_" + crypto.randomUUID() }
  try {
    await pauseDeNovoContinuation(scope)
    expect((await armDeNovoContinuation(scope, task)).state).toBe("paused")
    await resumeDeNovoContinuation(scope)
    expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
    await acknowledgeDeNovoContinuation(scope, task, "INTERRUPTED")
    expect((await readDeNovoContinuation(scope))?.state).toBe("observed")
    expect((await armDeNovoContinuation(scope, task, Date.now(), true)).state).toBe("waiting")
    await tickDeNovoContinuation(scope, { active: async () => true, idle: () => true,
      observe: async () => ({ ...task, state: "RECONCILE" }), wake: async () => {} })
    expect((await readDeNovoContinuation(scope))?.state).toBe("delivered")
    await acknowledgeDeNovoContinuation(scope, task, "RUNNING")
    expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
  } finally {
    await Storage.remove(["denovo-continuations", scope.project, scope.session])
    await Storage.remove(["denovo-continuation-stops", scope.project, scope.session])
  }
})

test("a real user turn settles an expired terminal continuation without repeating its notification", async () => {
  const scope = { project: "denovo-test", session: "ses_" + crypto.randomUUID() }
  const generation = { ...task, stage: "generation" as const, task: "generation-next", request: "generation-next" }
  let wakes = 0
  try {
    await armDeNovoContinuation(scope, task, 0)
    await tickDeNovoContinuation(scope, { active: async () => true, idle: () => true,
      observe: async () => ({ ...task, state: "COMPLETED" }), wake: async () => { wakes++ } }, 0)
    await tickDeNovoContinuation(scope, { active: async () => true, idle: () => true,
      observe: async () => ({ ...task, state: "COMPLETED" }), wake: async () => { wakes++ } }, 60_000)
    expect((await readDeNovoContinuation(scope))?.state).toBe("delivered")
    expect(wakes).toBe(2)
    await tickDeNovoContinuation(scope, { active: async () => true, idle: () => true,
      observe: async () => ({ ...task, state: "COMPLETED" }), wake: async () => { wakes++ } }, 24 * 60 * 60_000 + 1)
    expect(await readDeNovoContinuation(scope)).toMatchObject({ state: "paused", observed: "COMPLETED",
      error: "Expired or study no longer active" })

    // Expiry pauses do not create a stop tombstone. A real user turn must still
    // settle the already delivered terminal stage so its successor can register.
    await resumeDeNovoContinuation(scope)
    expect(await readDeNovoContinuation(scope)).toMatchObject({ state: "observed", observed: "COMPLETED" })
    expect((await armDeNovoContinuation(scope, generation)).stage).toBe("generation")
    expect(wakes).toBe(2)
  } finally {
    await Storage.remove(["denovo-continuations", scope.project, scope.session])
    await Storage.remove(["denovo-continuation-stops", scope.project, scope.session])
  }
})

test("foreign study and disconnect stay unknown; pause is not undone by repeated registration", async () => {
  const scope = { project: "denovo-test", session: "ses_" + crypto.randomUUID() }
  let wakes = 0
  const ports = { active: async () => true, idle: () => true,
    observe: async () => ({ ...task, study: "study-foreign", state: "COMPLETED" }),
    wake: async () => { wakes++ } }
  try {
    await armDeNovoContinuation(scope, task, 0)
    await tickDeNovoContinuation(scope, ports, 0)
    expect((await readDeNovoContinuation(scope))?.error).toBeDefined()
    expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
    await tickDeNovoContinuation(scope, { ...ports, observe: async () => { throw Error("offline") } }, 60_000)
    expect((await readDeNovoContinuation(scope))?.error).toBe("offline")
    await pauseDeNovoContinuation(scope)
    await armDeNovoContinuation(scope, task, 120_000)
    await tickDeNovoContinuation(scope, ports, 120_000)
    expect((await readDeNovoContinuation(scope))?.state).toBe("paused")
    expect(wakes).toBe(0)
  } finally { await Storage.remove(["denovo-continuations", scope.project, scope.session]) }
})
