import { fixture } from "./workflow-plan.fixture"
import { registeredTarget } from "./target"
import { expect, test } from "bun:test"
import path from "node:path"
import { tmpdir } from "../../../test/fixture/fixture"
import { Store } from "./store"
import { digest } from "./model"
import { readWorkflowExecution } from "./workflow-execution"
import { prepareWorkflowScreening, recoverWorkflowScreening, workflowScreeningIntent } from "./workflow-plan"


test("lost compiler reply restores the exact plan using only the backend operation, before any new environment reads", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const database = path.join(tmp.path, "tasks.sqlite")
  const first = new Store(database)
  expect(recoverWorkflowScreening(first, f.intent, f.scope, f.environment.source)).toBeNull()
  const prepared = prepareWorkflowScreening(first, f.intent, f.scope, f.environment)
  expect(prepared.created).toBe(true)
  expect(prepared.task.plan.research_inputs).toEqual(f.intent.research_inputs)
  expect(prepared.task.plan.max_records).toBe(20000)
  expect(prepared.task.plan.shortlist).toBe(5)
  expect(prepared.reference.intent_sha256).toBe(digest(f.intent))
  expect(prepared.task.approval).toBeNull()
  first.close()
  const restored = new Store(database)
  try {
    // No saved task ID, full reference, plan timestamp or plan digest supplied.
    const result = recoverWorkflowScreening(restored, f.intent, f.scope, f.environment.source)!
    expect(result.task.plan).toEqual(prepared.task.plan)
    expect(result.reference).toEqual(prepared.reference)
    const retry = prepareWorkflowScreening(restored, f.intent, f.scope, {
      ...f.environment, assets: {}, gpu: { ...f.environment.gpu, points_per_hour: 100000 },
      source: { ...f.environment.source, origin: { ...f.environment.source.origin, assistant_message_id: "restarted-assistant" } },
    })
    expect(retry.created).toBe(false)
    expect(retry.task.plan).toEqual(prepared.task.plan)
    expect(restored.list()).toHaveLength(1)
    expect(readWorkflowExecution(restored, result.reference, f.scope)).toMatchObject({
      approval_recorded: false, execution: { state: "not_submitted" }, assessment: { state: "not_evaluated" } })
    expect(restored.stages(result.task.id).every(stage => stage.handle === null && stage.submitted === null)).toBe(true)
  } finally { restored.close() }
})

test("recovery preserves cancellation and cannot switch session, original user, evidence, inputs or revision", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    const saved = prepareWorkflowScreening(store, f.intent, f.scope, f.environment)
    store.endApproval(saved.task.id, saved.task.digest, f.scope.sessionID, f.scope.directory, "Fixture refusal")
    expect(prepareWorkflowScreening(store, f.intent, f.scope, f.environment).task.state).toBe("CANCELLED")
    for (const intent of [{ ...f.intent, revision: 8 },
      { ...f.intent, scientific_review: { ...f.intent.scientific_review, reaction_scope: "Changed exploratory reaction assumptions; this must invalidate the old plan." } },
      { ...f.intent, screening_inputs: { ...f.intent.screening_inputs, manifest_sha256: "a".repeat(64) } },
      { ...f.intent, conditions: { ...f.intent.conditions, max_points: 20 } },
      { ...f.intent, receptor_evidence: { ...f.intent.receptor_evidence,
        artifact_id: "screening-receptor-" + "e".repeat(32), manifest_sha256: "e".repeat(64) } },
      { ...f.intent, research_inputs: { ...f.intent.research_inputs, reference: {
        ...f.intent.research_inputs.reference, sha256: "f".repeat(64) } } }])
      expect(() => prepareWorkflowScreening(store, intent, f.scope, f.environment)).toThrow("intent changed")
    expect(() => recoverWorkflowScreening(store, f.intent, { ...f.scope, sessionID: "foreign" }, f.environment.source)).toThrow("conversation")
    expect(() => recoverWorkflowScreening(store, f.intent, f.scope, { ...f.environment.source,
      origin: { ...f.environment.source.origin, user_message_id: "another-user" } })).toThrow("original user")
    expect(() => recoverWorkflowScreening(store, f.intent, f.scope, { ...f.environment.source, text: "restated by model" })).toThrow("host-bound")
    expect(store.list()).toHaveLength(1)
  } finally { store.close() }
})

test("failed provenance commit rolls back native task, stages and operation binding together", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    store.db.exec(`CREATE TRIGGER reject_workflow_intent BEFORE INSERT ON events
      WHEN NEW.kind='workflow_screening_intent_bound'
      BEGIN SELECT RAISE(ABORT,'fixture interrupted provenance commit'); END;`)
    expect(() => prepareWorkflowScreening(store, f.intent, f.scope, f.environment)).toThrow("interrupted provenance")
    expect(store.list()).toHaveLength(0)
    expect(recoverWorkflowScreening(store, f.intent, f.scope, f.environment.source)).toBeNull()
    store.db.exec("DROP TRIGGER reject_workflow_intent")
    expect(prepareWorkflowScreening(store, f.intent, f.scope, f.environment).created).toBe(true)
    expect(store.list()).toHaveLength(1)
  } finally { store.close() }
})

test("recovery refuses missing, duplicate or altered scientific-intent provenance", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    const saved = prepareWorkflowScreening(store, f.intent, f.scope, f.environment)
    const payload = { intent: f.intent, intent_sha256: digest(f.intent) }
    store.event(saved.task.id, "workflow_screening_intent_bound", payload)
    expect(() => recoverWorkflowScreening(store, f.intent, f.scope, f.environment.source)).toThrow("committed scientific intent")
    store.db.query("DELETE FROM events WHERE task_id=? AND kind='workflow_screening_intent_bound'").run(saved.task.id)
    expect(() => recoverWorkflowScreening(store, f.intent, f.scope, f.environment.source)).toThrow("committed scientific intent")
    store.event(saved.task.id, "workflow_screening_intent_bound", { ...payload, intent_sha256: "f".repeat(64) })
    expect(() => recoverWorkflowScreening(store, f.intent, f.scope, f.environment.source)).toThrow("committed scientific intent")
    expect(store.list()).toHaveLength(1)
  } finally { store.close() }
})

test("two host processes compile the same durable operation into one native task", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const database = path.join(tmp.path, "tasks.sqlite")
  // Initialize the existing schema before racing two separate writers.
  new Store(database).close()
  const input = path.join(tmp.path, "input.json")
  const script = path.join(tmp.path, "host.ts")
  await Bun.write(input, JSON.stringify(f))
  await Bun.write(script, `import { Store } from ${JSON.stringify(path.join(import.meta.dir, "store.ts"))};
import { prepareWorkflowScreening } from ${JSON.stringify(path.join(import.meta.dir, "workflow-plan.ts"))};
const f = await Bun.file(process.argv[2]).json();
const store = new Store(process.argv[3]);
try { const result = prepareWorkflowScreening(store, f.intent, f.scope, f.environment);
  console.log(JSON.stringify({ created: result.created, reference: result.reference, task: result.task.id }));
} finally { store.close(); }
`)
  const processes = [0, 1].map(() => Bun.spawn([process.execPath, script, input, database], {
    cwd: process.cwd(), stdout: "pipe", stderr: "pipe",
  }))
  const results = await Promise.all(processes.map(async child => {
    const [code, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()])
    expect(stderr).toBe("")
    expect(code).toBe(0)
    return JSON.parse(stdout) as { created: boolean; reference: unknown; task: string }
  }))
  expect(results.filter(result => result.created)).toHaveLength(1)
  expect(results[0].reference).toEqual(results[1].reference)
  expect(results[0].task).toEqual(results[1].task)
  const store = new Store(database)
  try {
    expect(store.list()).toHaveLength(1)
    expect(store.stages(results[0].task)).toHaveLength(4)
    expect(store.get(results[0].task).approval).toBeNull()
  } finally { store.close() }
}, 30000)

test("compiler refuses unbound receptor bytes, missing assets, unsupported targets and silent budget changes", async () => {
  await using tmp = await tmpdir({ git: true })
  const f = fixture(tmp.path)
  const store = new Store(path.join(tmp.path, "tasks.sqlite"))
  try {
    expect(() => prepareWorkflowScreening(store, { ...f.intent, receptor_evidence: {
      ...f.intent.receptor_evidence, receptor_sha256: "f".repeat(64) } }, f.scope, f.environment)).toThrow("receptor differs")
    expect(() => prepareWorkflowScreening(store, f.intent, f.scope, { ...f.environment, assets: {} })).toThrow()
    expect(() => prepareWorkflowScreening(store, { ...f.intent, conditions: { ...f.intent.conditions,
      target: "BTK", site: "C481", target_parameters: { ...registeredTarget, target: "BTK", residue_number: 481 } } },
      f.scope, f.environment)).toThrow()
    expect(() => prepareWorkflowScreening(store, { ...f.intent, conditions: { ...f.intent.conditions, max_points: 0 } },
      f.scope, f.environment)).toThrow()
    expect(() => workflowScreeningIntent.parse({ ...f.intent, conditions: { ...f.intent.conditions, screening_mode: "economy" } })).toThrow()
    expect(() => workflowScreeningIntent.parse({ ...f.intent, human_approved: true })).toThrow()
    expect(store.list()).toHaveLength(0)
  } finally { store.close() }
})
