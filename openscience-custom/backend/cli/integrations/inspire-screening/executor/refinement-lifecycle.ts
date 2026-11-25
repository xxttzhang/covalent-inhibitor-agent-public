import z from "zod"
import { digest, type Task } from "./model"
import type { Store } from "./store"
import { ensureDaemon } from "./host"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows, refinementWorkflow } from "./refinement-workflow"
import { RefinementRuns } from "./refinement-runs"
import { refinementCommand } from "./refinement-command"
import { verifyRefinementResult } from "./refinement-result"
import { approveRefinementExecution, refinementApprovalReference, refinementExecutions } from "./refinement-execution"
import { planningEvent, verifyPlanningScope } from "./refinement-planning-scope"

export const refinementObservation = z.object({ schema: z.literal("library-refinement-ledger-observation-v1"),
  reference: refinementApprovalReference, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/),
  native_state: z.enum(["NOT_CREATED", "DRAFT", "APPROVED", "CANCELLED"]), approval_recorded: z.boolean(),
  execution_state: z.enum(["not_submitted", "submitted", "running", "completed", "failed", "outcome_unknown"]),
  stages: z.array(z.object({ task_id: z.string().regex(/^refine-[a-f0-9]{24}$/), engine: z.enum(["af3", "covdock"]),
    state: z.enum(["NOT_CREATED", "DRAFT", "APPROVED", "WAITING", "DISPATCHING", "SUBMITTED", "QUEUED", "RUNNING",
      "VERIFYING", "NEEDS_REVIEW", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "SUCCEEDED"]),
    submission_reserved: z.boolean(), job_observed: z.boolean(), result_record_verified: z.boolean() }).strict()).length(2),
  ledger_sequence: z.number().int().positive(), observed_at: z.number().int().positive(),
  observation_scope: z.literal("persisted_executor_ledger"), remote_artifacts_rehashed: z.literal(false),
  scientific_assessment: z.literal("not_evaluated"),
}).strict()

/** Verify the original native records; this projection cannot prove fresh remote
 * availability or biological activity and never advances an execution. */
export function readRefinementObservation(store: Store, parent: Task, input: z.infer<typeof refinementApprovalReference>) {
  return store.db.transaction(() => {
    const reference = refinementApprovalReference.parse(input)
    verifyPlanningScope(store, parent, reference.planning_scope)
    const record = planningEvent(store, parent.id, "refinement_workflow_draft", reference.planning_scope)
    const saved = record ? JSON.parse(record.payload) : null
    const plan = refinementWorkflow.parse(saved?.workflow)
    if (plan.id !== reference.workflow_id || digest(plan) !== reference.digest || saved.digest !== reference.digest
      || plan.max_points > reference.planning_scope.max_points + 1e-8
      || plan.stages.some(stage => stage.parent_id !== parent.id || stage.parent_digest !== parent.digest
        || stage.directory !== parent.plan.directory || stage.session !== parent.plan.session
        || stage.selection_id !== reference.planning_scope.selection.selection_id
        || stage.selection_manifest_digest !== reference.planning_scope.selection.selection_manifest_digest))
      throw Error("Refinement execution differs from its bound plan, subset or parent")
    const workflows = new RefinementWorkflows(new Refinements(store))
    plan.stages.forEach(stage => workflows.refinements.parent(stage))
    const native = store.db.query("SELECT id FROM refinement_workflows WHERE id=?").get(plan.id) ? workflows.get(plan.id) : null
    if (native && native.digest !== reference.digest) throw Error("Native refinement plan changed")
    if (native?.state === "APPROVED") plan.stages.forEach(stage => workflows.authorize(stage.id))
    if (native && native.state !== "APPROVED" && native.approval) throw Error("Unapproved refinement has an inconsistent grant")
    const runs = new RefinementRuns(workflows.refinements)
    const stages = plan.stages.map(stage => {
      const exists = store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id)
      if (!native || native.state !== "APPROVED") {
        if (exists) throw Error("Unapproved refinement has execution records")
        return { task_id: stage.id, engine: stage.engine.name, state: native?.state ?? "NOT_CREATED",
          submission_reserved: false, job_observed: false, result_record_verified: false }
      }
      if (!exists) return { task_id: stage.id, engine: stage.engine.name, state: "APPROVED",
        submission_reserved: false, job_observed: false, result_record_verified: false }
      const run = runs.get(stage.id)
      if (digest(refinementCommand(stage, workflows.refinements.parent(stage))) !== run.command_digest
        || run.handle !== null && run.handle !== run.name
        || run.submitted === null && (run.handle !== null || ["DISPATCHING", "SUBMITTED", "QUEUED", "RUNNING", "VERIFYING", "SUCCEEDED"].includes(run.state))
        || ["SUBMITTED", "QUEUED", "RUNNING", "VERIFYING", "SUCCEEDED"].includes(run.state) && run.handle !== run.name
        || run.state === "WAITING" && run.submitted !== null)
        throw Error("Refinement command or execution provenance changed")
      if (run.state === "SUCCEEDED") {
        if (!run.manifest || run.handle !== run.name) throw Error("Refinement completion lacks its exact result")
        verifyRefinementResult(JSON.parse(run.manifest), stage)
      } else if (run.manifest) throw Error("Unfinished refinement cannot expose a completed result")
      return { task_id: stage.id, engine: stage.engine.name, state: run.state,
        submission_reserved: run.submitted !== null, job_observed: run.handle === run.name, result_record_verified: run.state === "SUCCEEDED" }
    })
    const state = native?.state !== "APPROVED" ? "not_submitted"
      : stages.every(stage => stage.state === "SUCCEEDED") ? "completed"
      : stages.every(stage => ["SUCCEEDED", "FAILED", "CANCELLED"].includes(stage.state)) ? "failed"
      : stages.some(stage => ["NEEDS_REVIEW", "DISPATCHING", "CANCEL_REQUESTED"].includes(stage.state)) ? "outcome_unknown"
      : stages.some(stage => ["RUNNING", "VERIFYING"].includes(stage.state)) ? "running"
      : stages.some(stage => ["SUBMITTED", "QUEUED"].includes(stage.state)) ? "submitted"
      : stages.some(stage => ["FAILED", "CANCELLED"].includes(stage.state)) ? "outcome_unknown" : "not_submitted"
    const sequence = store.db.query("SELECT MAX(sequence) AS sequence FROM events WHERE task_id=?").get(parent.id) as { sequence: number }
    return refinementObservation.parse({ schema: "library-refinement-ledger-observation-v1", reference, task_id: parent.id,
      native_state: native?.state ?? "NOT_CREATED", approval_recorded: native?.state === "APPROVED", execution_state: state,
      stages, ledger_sequence: sequence.sequence, observed_at: Date.now(), observation_scope: "persisted_executor_ledger",
      remote_artifacts_rehashed: false, scientific_assessment: "not_evaluated" })
  })()
}

/** Both the compatibility tool and research workflow use these original native
 * approval/cancellation/observer paths. A reference narrows cancellation to one
 * workflow; no model-supplied ID is accepted by the research adapter. */
export async function advanceNativeRefinement(store: Store, parent: Task, action: "approve" | "cancel" | "wake",
  context: Parameters<typeof approveRefinementExecution>[2], reference?: z.infer<typeof refinementApprovalReference>,
  observer: typeof ensureDaemon = ensureDaemon) {
  if (parent.plan.directory !== context.directory || parent.plan.session !== context.sessionID)
    throw Error("Refinement lifecycle belongs to another conversation")
  if (reference) readRefinementObservation(store, parent, reference)
  if (action === "approve") {
    const result = await approveRefinementExecution(store, parent, context, reference)
    await observer(store)
    return result
  }
  const workflows = new RefinementWorkflows(new Refinements(store))
  const runs = new RefinementRuns(workflows.refinements)
  const executions = reference ? [workflows.get(reference.workflow_id)] : refinementExecutions(store, parent).map(row => workflows.get(row.workflow_id))
  if (action === "wake" && (executions.length === 0 || executions.some(saved => saved.state !== "APPROVED")))
    throw Error("Only an approved refinement may resume its observer")
  store.db.transaction(() => {
    for (const execution of executions) {
      const saved = workflows.get(execution.id)
      if (reference && saved.digest !== reference.digest) throw Error("Refinement lifecycle plan changed")
      if (action === "cancel" && saved.state === "DRAFT") workflows.endApproval(saved.id, saved.digest, context)
      if (saved.state === "APPROVED") saved.plan.stages.forEach(stage => {
        workflows.authorize(stage.id)
        if (action === "cancel") { runs.ensure(stage.id); runs.cancel(stage.id, context.directory) }
      })
    }
  }).immediate()
  await observer(store)
  return null
}
