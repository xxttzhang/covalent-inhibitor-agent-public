import { expect, test } from "bun:test"
import path from "node:path"
import { tmpdir } from "../../../test/fixture/fixture"
import { compileScreening } from "./compile"
import { digest } from "./model"
import { requestHash } from "./request"
import { Store } from "./store"
import { closeWorkflowOperation, readWorkflowClosure, workflowOperation, workflowObservation, prepareWorkflowExecution, readWorkflowExecution, workflowTaskArguments, type WorkflowReference } from "./workflow-execution"
import { advanceNativeTask } from "./task-lifecycle"
import { approve } from "./approval"
import { Instance } from "../../../src/project/instance"
import { Session } from "../../../src/session"
import { PermissionNext } from "../../../src/permission/next"
import { Bus } from "../../../src/bus"

function fixture(directory: string, sessionID = "workflow-test") {
  const text = "Software fixture; do not submit scientific compute"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
    image: "fixture", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0 }
  const plan = compileScreening({ directory, session: sessionID,
    source: { text, origin: { user_message_id: "user-fixture", assistant_message_id: "assistant-fixture",
      part_ids: ["text-fixture"], request_sha256: requestHash(text) } },
    intent: { target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv", screening_mode: "economy", max_points: 10,
      target_parameters: { target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
        insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" } },
    assets: Object.fromEntries(["source", "target", "receptor", "engine", "worker"].map(name => ["/inspire/fixture/" + name, "a".repeat(64)])),
    runtime: { root: "/inspire/fixture/outputs", project: "/inspire/fixture/science", worker: "/inspire/fixture/worker.py", activation: "/inspire/fixture/env.sh" },
    connection: { cli: "/nonexistent/inspire-fixture", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    cpu, gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 1 },
  })
  const ref: WorkflowReference = { schema: "library-workflow-execution-reference-v1", workflow_id: "library-flow-" + "a".repeat(24),
    revision: 4, operation_id: "b".repeat(32), intent_sha256: "c".repeat(64), plan_sha256: digest(plan) }
  return { plan, ref, scope: { directory, sessionID } }
}

test("lost preparation response recovers one unapproved task across independent SQLite connections", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const file = path.join(tmp.path, "tasks.sqlite")
  const first = new Store(file)
  expect(readWorkflowExecution(first, f.ref, f.scope)).toBeNull()
  const result = prepareWorkflowExecution(first, f.ref, f.plan, f.scope)
  expect(result.created).toBe(true)
  expect(result.task.approval).toBeNull()
  first.close() // Simulates the host losing its reply before the backend commits it.
  const second = new Store(file)
  const third = new Store(file)
  try {
    for (const store of [second, third]) {
      const restored = prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
      expect(restored.created).toBe(false)
      expect(restored.task.id).toBe(result.task.id)
      expect(store.list()).toHaveLength(1)
      expect(readWorkflowExecution(store, f.ref, f.scope)).toMatchObject({
        task_id: f.plan.id, approval_recorded: false, execution: { state: "not_submitted" },
        assessment: { state: "not_evaluated" }, compute_submitted_by_adapter: false })
      expect(workflowTaskArguments(store, f.ref, f.scope, { action: "status" })).toEqual({ action: "status", task_id: f.plan.id })
    }
  } finally { second.close(); third.close() }
})

test("failed binding insert rolls back task and stages; retry retains the exact frozen plan", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
    const next = fixture(tmp.path)
    next.ref.operation_id = "d".repeat(32)
    store.db.exec(`CREATE TRIGGER abort_workflow_binding BEFORE INSERT ON workflow_task_bindings
      BEGIN SELECT RAISE(ABORT,'fixture crash at binding commit'); END;`)
    expect(() => prepareWorkflowExecution(store, next.ref, next.plan, next.scope)).toThrow("fixture crash")
    expect(store.list()).toHaveLength(1)
    expect(store.stages(next.plan.id)).toEqual([])
    expect(readWorkflowExecution(store, next.ref, next.scope)).toBeNull()
    store.db.exec("DROP TRIGGER abort_workflow_binding")
    expect(prepareWorkflowExecution(store, next.ref, next.plan, next.scope).created).toBe(true)
    expect(store.list()).toHaveLength(2)
  } finally { store.close() }
})

test("workflow binding rejects stale intent, changed plan, task adoption and identity injection", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
    for (const ref of [{ ...f.ref, revision: 5 }, { ...f.ref, intent_sha256: "e".repeat(64) }, { ...f.ref, plan_sha256: "f".repeat(64) }])
      expect(() => readWorkflowExecution(store, ref, f.scope)).toThrow()
    expect(() => readWorkflowExecution(store, f.ref, { ...f.scope, sessionID: "another-session" })).toThrow()
    expect(readWorkflowExecution(store, f.ref, { ...f.scope, directory: tmp.path + "/other" })).toBeNull()
    const other = fixture(tmp.path)
    const altered = { ...f.ref, plan_sha256: digest(other.plan) }
    expect(() => prepareWorkflowExecution(store, altered, other.plan, f.scope)).toThrow("reference")
    store.create(other.plan)
    expect(() => prepareWorkflowExecution(store, { ...other.ref, operation_id: "d".repeat(32) }, other.plan, f.scope)).toThrow("reconcile")
    for (const input of [{ action: "create" }, { action: "status", task_id: other.plan.id },
      { action: "approve", human_approved: true }, { action: "status", workflow_id: "forged" }])
      expect(() => workflowTaskArguments(store, f.ref, f.scope, input)).toThrow()
    store.db.query("UPDATE workflow_task_bindings SET task_id=?").run(other.plan.id)
    expect(() => readWorkflowExecution(store, f.ref, f.scope)).toThrow("frozen task")
  } finally { store.close() }
})

test("execution receipt requires stage evidence and never promotes approval into running or scientific success", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
    store.approve(f.plan.id, digest(f.plan), f.scope.sessionID, f.scope.directory)
    expect(readWorkflowExecution(store, f.ref, f.scope)?.execution.state).toBe("not_submitted")
    store.state(f.plan.id, "SUCCEEDED") // Corrupt success label without any real evidence.
    expect(() => readWorkflowExecution(store, f.ref, f.scope)).toThrow("complete stage evidence")
    store.state(f.plan.id, "NEEDS_REVIEW", "Remote observation unavailable")
    expect(readWorkflowExecution(store, f.ref, f.scope)).toMatchObject({ execution: { state: "outcome_unknown" },
      assessment: { state: "not_evaluated" } })
    store.stage(f.plan.id, 0, { state: "SUCCEEDED" })
    expect(() => readWorkflowExecution(store, f.ref, f.scope)).toThrow("verified manifest")
  } finally { store.close() }
})

test("native refusal remains attached to the same workflow task after restart", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const f = fixture(tmp.path, session.id)
    const file = path.join(tmp.path, "tasks.sqlite")
    const store = new Store(file)
    const requested = Promise.withResolvers<PermissionNext.Request>()
    const controller = new AbortController()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => {
      if (event.properties.sessionID === session.id) requested.resolve(event.properties)
    })
    try {
      prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
      const native = approve(store, f.plan.id, { ...f.scope, abort: controller.signal,
        metadata() {}, ask: input => PermissionNext.ask({ ...input, sessionID: session.id, signal: controller.signal,
          ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }) })
        .then(() => ({ error: undefined }), (error: unknown) => ({ error }))
      const card = await Promise.race([requested.promise, native.then(result => {
        throw result.error ?? Error("Workflow preparation bypassed native approval")
      })])
      expect(card.patterns).toEqual([f.ref.plan_sha256])
      await PermissionNext.reply({ requestID: card.id, reply: "reject" })
      expect((await native).error).toBeDefined()
    } finally { controller.abort(); unsubscribe(); store.close() }
    const restored = new Store(file)
    try {
      expect(prepareWorkflowExecution(restored, f.ref, f.plan, f.scope).created).toBe(false)
      expect(readWorkflowExecution(restored, f.ref, f.scope)).toMatchObject({ task_state: "CANCELLED",
        approval_recorded: false, execution: { state: "not_submitted" }, blockers: [{ code: "task_cancelled" }] })
      expect(restored.stages(f.plan.id).every(stage => stage.submitted === null)).toBe(true)
    } finally { restored.close() }
  } })
}, 30000)

test("closed operation survives restart and prevents late compilation even when no task existed", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const { plan_sha256: _, ...operation } = f.ref
  const file = path.join(tmp.path, "tasks.sqlite")
  const first = new Store(file)
  const late = new Store(file)
  try {
    const closure = closeWorkflowOperation(first, workflowOperation.parse(operation), f.scope, "Revise scientific scope")
    expect(closure).toMatchObject({ state: "NO_TASK", task_id: null, compute_submitted: false })
    expect(readWorkflowClosure(late, operation, f.scope)).toEqual(closure)
    expect(() => prepareWorkflowExecution(late, f.ref, f.plan, f.scope)).toThrow("closed")
    expect(late.list()).toHaveLength(0)
    expect(() => readWorkflowClosure(late, { ...operation, intent_sha256: "f".repeat(64) }, f.scope)).toThrow("another intent")
    expect(() => readWorkflowClosure(late, operation, { ...f.scope, sessionID: "foreign" })).toThrow()
  } finally { first.close(); late.close() }
})

test("VS migration seals only cancelled unsubmitted work and retains its historical approval across restart", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const { plan_sha256: _, ...operation } = f.ref
  const file = path.join(tmp.path, "tasks.sqlite")
  const store = new Store(file)
  const reason = "Replace the suspended screening engine with a separately approved VS plan"
  try {
    expect(() => closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")).toThrow("original cancelled")
    prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
    expect(() => closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")).toThrow("cancelled, idle")
    store.approve(f.plan.id, f.ref.plan_sha256, f.scope.sessionID, f.scope.directory)
    for (const state of ["APPROVED", "ACTIVE", "CANCEL_REQUESTED", "NEEDS_REVIEW", "FAILED"] as const) {
      store.state(f.plan.id, state)
      expect(() => closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")).toThrow("cancelled, idle")
      expect(readWorkflowClosure(store, operation, f.scope)).toBeNull()
    }
    store.state(f.plan.id, "CANCELLED", "No computation submitted")
    const approval = store.get(f.plan.id).approval
    expect(() => closeWorkflowOperation(store, operation, f.scope, reason)).toThrow("unapproved")
    for (const change of [{ state: "DISPATCHING" as const }, { handle: "remote-fixture" }, { submitted: 1 },
      { state: "FAILED" as const }]) {
      store.stage(f.plan.id, 0, change)
      expect(() => closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")).toThrow("cancelled, idle")
      store.stage(f.plan.id, 0, { state: "WAITING", handle: null, submitted: null })
    }
    store.acquire(f.plan.id, "other-observer")
    expect(() => closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")).toThrow("cancelled, idle")
    store.release(f.plan.id, "other-observer")
    expect(() => closeWorkflowOperation(store, operation, { ...f.scope, sessionID: "foreign" }, reason, "migrate_to_vs")).toThrow()
    const closure = closeWorkflowOperation(store, operation, f.scope, reason, "migrate_to_vs")
    expect(closure).toMatchObject({ task_id: f.plan.id, state: "CANCELLED_UNSUBMITTED_FOR_VS", compute_submitted: false })
    expect(store.get(f.plan.id)).toMatchObject({ state: "CANCELLED", approval })
    const recovered = new Store(file)
    try {
      expect(closeWorkflowOperation(recovered, operation, f.scope, reason, "migrate_to_vs")).toEqual(closure)
      expect(() => closeWorkflowOperation(recovered, operation, f.scope, reason)).toThrow("another purpose")
      expect(() => prepareWorkflowExecution(recovered, f.ref, f.plan, f.scope)).toThrow("closed")
      expect(() => recovered.approve(f.plan.id, f.ref.plan_sha256, f.scope.sessionID, f.scope.directory)).toThrow("unchanged draft")
      expect(() => recovered.claim(f.plan.id, 0, "late-worker")).toThrow("approval/lease")
      expect(recovered.list()).toHaveLength(1)
      expect(recovered.get(f.plan.id).approval).toBe(approval)
    } finally { recovered.close() }
  } finally { store.close() }
})

test("closing a draft during native approval prevents a late grant from dispatching it", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const f = fixture(tmp.path, session.id)
    const { plan_sha256: _, ...operation } = f.ref
    const file = path.join(tmp.path, "tasks.sqlite")
    const store = new Store(file)
    const other = new Store(file)
    const requested = Promise.withResolvers<PermissionNext.Request>()
    const controller = new AbortController()
    const counters = { observers: 0 }
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => {
      if (event.properties.sessionID === session.id) requested.resolve(event.properties)
    })
    try {
      prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
      const native = advanceNativeTask(store, f.plan.id, "approve", { ...f.scope, abort: controller.signal,
        metadata() {}, ask: input => PermissionNext.ask({ ...input, sessionID: session.id, signal: controller.signal,
          ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }) }, async () => { counters.observers++ })
        .then(() => ({ error: undefined }), (error: unknown) => ({ error }))
      const card = await Promise.race([requested.promise, native.then(result => {
        throw result.error ?? Error("Native permission was bypassed")
      })])
      expect(card.patterns).toEqual([f.ref.plan_sha256])
      expect(closeWorkflowOperation(other, operation, f.scope, "Withdraw while approval pending").state).toBe("CANCELLED_UNAPPROVED")
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect((await native).error).toBeDefined()
      expect(counters.observers).toBe(0)
      expect(store.get(f.plan.id).approval).toBeNull()
      expect(workflowObservation(store, f.ref, f.scope)).toMatchObject({ task_state: "CANCELLED", execution_state: "not_submitted" })
      expect(() => prepareWorkflowExecution(store, f.ref, f.plan, f.scope)).toThrow("closed")
      expect(store.list()).toHaveLength(1)
    } finally { controller.abort(); unsubscribe(); store.close(); other.close() }
  } })
}, 30000)

test("native grant, wake, status and cancellation reuse the task; approved operations cannot be released", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const f = fixture(tmp.path, session.id)
    const { plan_sha256: _, ...operation } = f.ref
    const store = new Store(path.join(tmp.path, "tasks.sqlite"))
    const requested = Promise.withResolvers<PermissionNext.Request>()
    const controller = new AbortController()
    const counters = { observers: 0 }
    const observer = async () => { counters.observers++ } // Never start a real remote observer in software tests.
    const context = { ...f.scope, abort: controller.signal, metadata() {},
      ask: (input: Parameters<Parameters<typeof approve>[2]["ask"]>[0]) => PermissionNext.ask({ ...input,
        sessionID: session.id, signal: controller.signal, ruleset: [{ permission: "*", pattern: "*", action: "allow" as const }] }) }
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => {
      if (event.properties.sessionID === session.id) requested.resolve(event.properties)
    })
    try {
      prepareWorkflowExecution(store, f.ref, f.plan, f.scope)
      await expect(advanceNativeTask(store, f.plan.id, "wake", context, observer)).rejects.toThrow("Unapproved")
      const pending = advanceNativeTask(store, f.plan.id, "approve", context, observer)
      const card = await requested.promise
      expect(card.patterns).toEqual([f.ref.plan_sha256])
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect((await pending).state).toBe("APPROVED")
      const approved = workflowObservation(store, f.ref, f.scope)
      expect(approved).toMatchObject({ approval_recorded: true, execution_state: "not_submitted", assessment: "not_evaluated" })
      await advanceNativeTask(store, f.plan.id, "status", context, observer)
      expect(counters.observers).toBe(1)
      await advanceNativeTask(store, f.plan.id, "wake", context, observer)
      await advanceNativeTask(store, f.plan.id, "cancel", context, observer)
      expect(counters.observers).toBe(3)
      const cancelled = workflowObservation(store, f.ref, f.scope)
      expect(cancelled).toMatchObject({ task_state: "CANCEL_REQUESTED", execution_state: "outcome_unknown", remote_artifacts_rehashed: false })
      expect(cancelled.ledger_sequence).toBeGreaterThan(approved.ledger_sequence)
      expect(() => closeWorkflowOperation(store, operation, f.scope, "Unsafe release attempt")).toThrow("Only an unapproved")
      expect(store.stages(f.plan.id).every(stage => stage.handle === null && stage.submitted === null)).toBe(true)
    } finally { controller.abort(); unsubscribe(); store.close() }
  } })
}, 30000)
