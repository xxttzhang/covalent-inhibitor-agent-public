import z from "zod"
import { digest, type Task } from "./model"
import { selectionBinding } from "./selection-proposal"
import { workflowReference, readWorkflowExecution } from "./workflow-execution"
import type { Store } from "./store"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"

/** Internal research operation, supplied only after authenticated backend
 * handoff. A proposed cap and scientific rationale are never approval. */
export const refinementPlanningScope = z.object({ schema: z.literal("bound-refinement-planning-v1"),
  operation_id: z.string().regex(/^[a-f0-9]{32}$/), parent: workflowReference, selection: selectionBinding,
  max_points: z.number().finite().nonnegative(), scientific_review: z.string().trim().min(20).max(4000),
}).strict()
export type RefinementPlanningScope = z.infer<typeof refinementPlanningScope>

export function verifyPlanningScope(store: Store, task: Task, value: RefinementPlanningScope) {
  const scope = refinementPlanningScope.parse(value)
  if (readRefinementPlanningClosure(store, task.id, scope)) throw Error("Refinement planning operation is closed")
  const execution = readWorkflowExecution(store, scope.parent, { directory: task.plan.directory, sessionID: task.plan.session })
  if (!execution || execution.task_id !== task.id || execution.task_state !== "SUCCEEDED" || task.digest !== scope.parent.plan_sha256)
    throw Error("Refinement planning requires this workflow's verified completed parent")
  return scope
}

export function planningEvent(store: Store, task: string, kind: "refinement_execution_proposal" | "refinement_workflow_draft",
  input?: RefinementPlanningScope) {
  const scope = input ? refinementPlanningScope.parse(input) : undefined
  const row = scope ? store.db.query("SELECT at,payload FROM events WHERE task_id=? AND kind=? AND json_extract(payload,'$.planning_scope.operation_id')=? ORDER BY sequence DESC LIMIT 1")
    .get(task, kind, scope.operation_id) : store.db.query("SELECT at,payload FROM events WHERE task_id=? AND kind=? ORDER BY sequence DESC LIMIT 1").get(task, kind)
  const result = row as { at: number; payload: string } | null
  if (scope && result && digest(JSON.parse(result.payload).planning_scope) !== digest(scope))
    throw Error("Refinement planning operation belongs to different candidates, review or budget")
  return result
}

/** A bound operation gets one durable proposal and one durable draft, in the
 * existing ledger. Concurrent hosts retain the winner; recovery never creates
 * fresh plan identities. Explicit replanning needs a new backend operation. */
export function retainPlanningEvent(store: Store, task: string, kind: "refinement_execution_proposal" | "refinement_workflow_draft",
  payload: Record<string, unknown>, scope?: RefinementPlanningScope) {
  return store.db.transaction(() => {
    if (scope && readRefinementPlanningClosure(store, task, scope)) throw Error("Refinement planning operation is closed")
    if (scope && planningEvent(store, task, kind, scope)) return
    store.event(task, kind, { ...payload, ...(scope ? { planning_scope: scope } : {}) })
  }).immediate()
}

export const refinementPlanningClosure = z.object({ schema: z.literal("library-refinement-planning-closure-v1"),
  planning_scope: refinementPlanningScope, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/),
  reason: z.string().trim().min(5).max(2000), state: z.literal("NO_EXECUTABLE_WORKFLOW"), compute_submitted: z.literal(false),
}).strict()

export function readRefinementPlanningClosure(store: Store, task: string, input: RefinementPlanningScope) {
  const row = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='refinement_planning_closed' AND json_extract(payload,'$.planning_scope.operation_id')=? ORDER BY sequence DESC LIMIT 1")
    .get(task, input.operation_id) as { payload: string } | null
  if (!row) return null
  const value = refinementPlanningClosure.parse(JSON.parse(row.payload))
  if (value.task_id !== task || digest(value.planning_scope) !== digest(input)) throw Error("Closed refinement operation belongs to another scope")
  return value
}

/** Seal planning in the same ledger transaction as proposal/draft retention.
 * A native confirmation or approved workflow requires its separate lifecycle. */
export function closeRefinementPlanning(store: Store, task: Task, scope: RefinementPlanningScope, reason: string) {
  return store.db.transaction(() => {
    const previous = readRefinementPlanningClosure(store, task.id, scope)
    if (previous) return previous
    verifyPlanningScope(store, task, scope)
    const draft = planningEvent(store, task.id, "refinement_workflow_draft", scope)
    const id = draft ? JSON.parse(draft.payload).workflow?.id : null
    if (id && store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()
      && store.db.query("SELECT id FROM refinement_workflows WHERE id=?").get(id)) {
      const workflow = new RefinementWorkflows(new Refinements(store)).get(id)
      const saved = JSON.parse(draft!.payload)
      if (workflow.digest !== saved.digest || workflow.state !== "CANCELLED" || workflow.approval
        || store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()
          && workflow.plan.stages.some(stage => store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id)))
        throw Error("Native refinement confirmation exists; reconcile its lifecycle before releasing research planning")
    }
    const closure = refinementPlanningClosure.parse({ schema: "library-refinement-planning-closure-v1", planning_scope: scope,
      task_id: task.id, reason, state: "NO_EXECUTABLE_WORKFLOW", compute_submitted: false })
    store.event(task.id, "refinement_planning_closed", closure)
    return closure
  }).immediate()
}

export function assertRefinementDraftOpen(store: Store, task: string, workflow: string) {
  const rows = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='refinement_workflow_draft' AND json_extract(payload,'$.workflow.id')=?")
    .all(task, workflow) as { payload: string }[]
  for (const row of rows) {
    const value = JSON.parse(row.payload)
    if (value.planning_scope && readRefinementPlanningClosure(store, task, refinementPlanningScope.parse(value.planning_scope)))
      throw Error("Released refinement planning cannot create a native confirmation")
  }
}
