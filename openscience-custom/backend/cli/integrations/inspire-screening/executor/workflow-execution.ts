import z from "zod"
import { digest, planSchema, type Plan } from "./model"
import type { Store } from "./store"
import { verifiedExecution } from "./execution-evidence"

/** Supplied by the host after reading a backend workflow intent, never by the
 * model's executor arguments. This reference grants no execution authority. */
export const workflowReference = z.object({
  schema: z.literal("library-workflow-execution-reference-v1"),
  workflow_id: z.string().regex(/^library-flow-[a-f0-9]{24}$/),
  revision: z.number().int().positive(), operation_id: z.string().regex(/^[a-f0-9]{32}$/),
  intent_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  plan_sha256: z.string().regex(/^[a-f0-9]{64}$/),
}).strict()
export type WorkflowReference = z.infer<typeof workflowReference>
export const workflowOperation = workflowReference.omit({ plan_sha256: true })
export type WorkflowOperation = z.infer<typeof workflowOperation>
export const workflowClosure = z.object({ schema: z.literal("library-workflow-operation-closure-v1"),
  operation: workflowOperation, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/).nullable(),
  state: z.enum(["NO_TASK", "CANCELLED_UNAPPROVED", "CANCELLED_UNSUBMITTED_FOR_VS"]), reason: z.string().trim().min(5).max(2000),
  compute_submitted: z.literal(false) }).strict()
type Scope = { directory: string; sessionID: string }
type Row = { reference: string; reference_digest: string; task_id: string; session_id: string }

function table(store: Store) {
  store.db.exec(`CREATE TABLE IF NOT EXISTS workflow_task_bindings(
    directory TEXT NOT NULL, workflow_id TEXT NOT NULL, operation_id TEXT NOT NULL,
    reference TEXT NOT NULL, reference_digest TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL, PRIMARY KEY(directory,workflow_id,operation_id));`)
}

export function readWorkflowClosure(store: Store, input: WorkflowOperation, scope: Scope) {
  const operation = workflowOperation.parse(input)
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='workflow_operation_closures'").get()) return null
  const row = store.db.query("SELECT session_id,receipt FROM workflow_operation_closures WHERE directory=? AND workflow_id=? AND operation_id=?")
    .get(scope.directory, operation.workflow_id, operation.operation_id) as { session_id: string; receipt: string } | null
  if (!row) return null
  const receipt = workflowClosure.parse(JSON.parse(row.receipt))
  if (row.session_id !== scope.sessionID || digest(receipt.operation) !== digest(operation))
    throw Error("Closed workflow operation belongs to another intent or conversation")
  return receipt
}

/** Seal before releasing the backend lock. In-flight compilers serialize
 * against this tombstone in the SAME database, including the no-task case. */
export function closeWorkflowOperation(store: Store, input: WorkflowOperation, scope: Scope, reason: string,
  purpose: "release" | "migrate_to_vs" = "release") {
  const operation = workflowOperation.parse(input)
  workflowClosure.shape.reason.parse(reason)
  store.db.exec(`CREATE TABLE IF NOT EXISTS workflow_operation_closures(directory TEXT NOT NULL,
    workflow_id TEXT NOT NULL, operation_id TEXT NOT NULL, session_id TEXT NOT NULL, receipt TEXT NOT NULL,
    PRIMARY KEY(directory,workflow_id,operation_id));`)
  return store.db.transaction(() => {
    const prior = readWorkflowClosure(store, operation, scope)
    if (prior) {
      if ((prior.state === "CANCELLED_UNSUBMITTED_FOR_VS") !== (purpose === "migrate_to_vs"))
        throw Error("Workflow operation was closed for another purpose")
      return prior
    }
    const saved = recoverWorkflowOperation(store, operation, scope)
    if (purpose === "migrate_to_vs" && !saved) throw Error("VS migration requires the original cancelled native task")
    if (saved) {
      const verified = verifiedExecution(store, saved.task.id)
      if (purpose === "migrate_to_vs") {
        if (verified.task.state !== "CANCELLED" || verified.task.lease_until > Date.now()
          || verified.stages.some(stage => stage.state !== "WAITING" || stage.handle !== null
            || stage.submitted !== null || stage.manifest !== null))
          throw Error("VS migration requires a cancelled, idle task with every stage unsubmitted and waiting")
        // Retain the old exact-plan approval and cancellation in the ledger.
        // This closure never creates a replacement task or transfers authority.
      } else {
        if (verified.task.approval || !["DRAFT", "CANCELLED"].includes(verified.task.state)
          || verified.stages.some(stage => stage.handle || stage.submitted !== null || stage.manifest))
          throw Error("Only an unapproved, never-submitted operation may be released; cancel and reconcile approved work")
        const ended = store.endApproval(saved.task.id, saved.task.digest, scope.sessionID, scope.directory, reason)
        if (ended.state !== "CANCELLED" || ended.approval) throw Error("Native draft was not safely closed")
      }
    }
    const receipt = workflowClosure.parse({ schema: "library-workflow-operation-closure-v1", operation,
      task_id: saved?.task.id ?? null, state: purpose === "migrate_to_vs" ? "CANCELLED_UNSUBMITTED_FOR_VS"
        : saved ? "CANCELLED_UNAPPROVED" : "NO_TASK", reason, compute_submitted: false })
    store.db.query("INSERT INTO workflow_operation_closures VALUES(?,?,?,?,?)")
      .run(scope.directory, operation.workflow_id, operation.operation_id, scope.sessionID, JSON.stringify(receipt))
    return receipt
  }).immediate()
}

function binding(store: Store, ref: WorkflowReference, scope: Scope) {
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='workflow_task_bindings'").get()) return null
  const row = store.db.query("SELECT reference,reference_digest,task_id,session_id FROM workflow_task_bindings WHERE directory=? AND workflow_id=? AND operation_id=?")
    .get(scope.directory, ref.workflow_id, ref.operation_id) as Row | null
  if (!row) return null
  const saved = workflowReference.parse(JSON.parse(row.reference))
  if (row.session_id !== scope.sessionID || digest(saved) !== row.reference_digest || digest(ref) !== row.reference_digest)
    throw Error("Workflow execution reference, revision or conversation changed")
  const task = store.get(row.task_id)
  if (task.digest !== ref.plan_sha256 || task.plan.directory !== scope.directory || task.plan.session !== scope.sessionID)
    throw Error("Workflow execution differs from its frozen task or project")
  return task
}

/** Recovery starts with the backend's durable operation, which predates plan
 * compilation. A lost host reply must not require the caller to invent the
 * random plan ID, timestamp or digest it never received. */
export function recoverWorkflowOperation(store: Store, input: WorkflowOperation, scope: Scope) {
  const operation = workflowOperation.parse(input)
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='workflow_task_bindings'").get()) return null
  const row = store.db.query("SELECT reference FROM workflow_task_bindings WHERE directory=? AND workflow_id=? AND operation_id=?")
    .get(scope.directory, operation.workflow_id, operation.operation_id) as { reference: string } | null
  if (!row) return null
  const reference = workflowReference.parse(JSON.parse(row.reference))
  const saved = { schema: reference.schema, workflow_id: reference.workflow_id, revision: reference.revision,
    operation_id: reference.operation_id, intent_sha256: reference.intent_sha256 }
  if (digest(saved) !== digest(operation))
    throw Error("Workflow operation revision or intent changed; reconcile its original operation")
  const task = binding(store, reference, scope)!
  return { reference, task }
}

/** The backend persists its pending intent first. Draft and binding then commit
 * in ONE existing executor transaction. Lost replies recover this operation;
 * they never compile a new plan or adopt an unrelated historical task. */
export function prepareWorkflowExecution(store: Store, input: WorkflowReference, value: Plan, scope: Scope) {
  const ref = workflowReference.parse(input)
  const plan = planSchema.parse(value)
  if (plan.directory !== scope.directory || plan.session !== scope.sessionID || !plan.origin)
    throw Error("Workflow preparation requires its own host-bound request and conversation")
  if (digest(plan) !== ref.plan_sha256) throw Error("Workflow frozen plan digest changed")
  table(store)
  return store.db.transaction(() => {
    if (readWorkflowClosure(store, workflowOperation.parse({ schema: ref.schema, workflow_id: ref.workflow_id,
      revision: ref.revision, operation_id: ref.operation_id, intent_sha256: ref.intent_sha256 }), scope))
      throw Error("Workflow operation is closed; never recreate or approve its task")
    const prior = binding(store, ref, scope)
    if (prior) return { task: prior, created: false, compute_submitted_by_adapter: false as const }
    if (Date.now() > plan.approve_before) throw Error("Workflow execution plan expired before preparation")
    if (store.db.query("SELECT id FROM tasks WHERE id=?").get(plan.id))
      throw Error("Existing task lacks this workflow binding; reconcile instead of adopting it")
    const task = store.create(plan)
    store.db.query("INSERT INTO workflow_task_bindings VALUES(?,?,?,?,?,?,?)")
      .run(scope.directory, ref.workflow_id, ref.operation_id, JSON.stringify(ref), digest(ref), task.id, scope.sessionID)
    store.event(task.id, "workflow_bound", { reference: ref, scope, plan_digest: task.digest })
    return { task, created: true, compute_submitted_by_adapter: false as const }
  }).immediate()
}

/** Compact, internally verified point-in-time ledger observation. Raw files
 * and native manifests stay in the existing executor; no scientific pass. */
export function workflowObservation(store: Store, ref: WorkflowReference, scope: Scope) {
  return store.db.transaction(() => {
    const result = readWorkflowExecution(store, ref, scope)
    if (!result) throw Error("No native task for this workflow operation")
    const row = store.db.query("SELECT MAX(sequence) AS sequence FROM events WHERE task_id=?").get(result.task_id) as { sequence: number }
    return { schema: "library-workflow-ledger-observation-v1" as const, reference: result.reference,
      task_id: result.task_id, task_state: result.task_state, approval_recorded: result.approval_recorded,
      execution_state: result.execution.state, ledger_sequence: row.sequence, observed_at: result.execution.observed_at,
      observation_scope: "persisted_executor_ledger" as const, assessment: "not_evaluated" as const,
      remote_artifacts_rehashed: false as const }
  })()
}

/** Read-only reconciliation. A missing binding proves only that this host has
 * no committed task for the operation, not that another host never ran it. */
export function readWorkflowExecution(store: Store, input: WorkflowReference, scope: Scope) {
  const ref = workflowReference.parse(input)
  const bound = binding(store, ref, scope)
  if (!bound) return null
  const { task, stages } = verifiedExecution(store, bound.id)
  const dispatched = stages.some(stage => stage.submitted !== null || stage.handle !== null)
  const state = task.state === "SUCCEEDED" ? "completed"
    : ["NEEDS_REVIEW", "CANCEL_REQUESTED"].includes(task.state) ? "outcome_unknown"
    : task.state === "FAILED" || task.state === "CANCELLED" && dispatched ? "failed"
    : stages.some(stage => stage.state === "RUNNING") ? "running"
    : dispatched ? "submitted" : "not_submitted"
  return { schema: "library-workflow-execution-receipt-v1" as const, reference: ref,
    task_id: task.id, plan_sha256: task.digest, task_state: task.state,
    execution: { scope: "registered_library_screening", state, task_ids: [task.id],
      observation_scope: "persisted_executor_ledger", observed_at: Date.now() },
    assessment: { scope: "drug_design_validation", state: "not_evaluated" as const,
      reason: "Execution receipts do not establish scientific acceptance or inhibition." },
    approval_recorded: task.approval !== null, stages,
    blockers: task.state === "CANCELLED" ? [{ code: "task_cancelled", reason: task.error }] :
      state === "outcome_unknown" ? [{ code: "execution_reconciliation_required", reason: task.error }] : [],
    remote_artifacts_rehashed: false as const, compute_submitted_by_adapter: false as const }
}

/** Bind task identity for the SAME native executor. Model-facing schemas never
 * accept a workflow reference or task override through this host-only path. */
export const workflowLifecycle = z.object({ action: z.enum(["approve", "status", "cancel", "wake", "report", "resume_delivery",
    "select_refinement", "diagnose_preparation", "diagnose_refinement", "plan_refinement", "approve_refinement",
    "cancel_refinement", "refinement_status", "refinement_report", "resume_refinement_delivery"]),
    selection_policy: z.unknown().optional() }).strict()

export function workflowTaskArguments(store: Store, ref: WorkflowReference, scope: Scope, input: unknown) {
  const args = workflowLifecycle.parse(input)
  const receipt = readWorkflowExecution(store, ref, scope)
  if (!receipt) throw Error("No task is bound to this workflow operation; reconcile preparation first")
  return { ...args, task_id: receipt.task_id }
}
