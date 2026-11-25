import z from "zod"
import type { Store } from "./store"
import { digest, type Task } from "./model"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows, refinementWorkflow } from "./refinement-workflow"
import { RefinementRuns } from "./refinement-runs"
import { RefinementRunner } from "./refinement-runner"
import { refinementTransport } from "./refinement-inspire"
import { Inspire } from "./inspire"
import { admitRefinement } from "./refinement-admission"
import { proposeVerifiedRefinementWorkflow } from "./selection-evidence"
import { refinementDraftSnapshot } from "./refinement-builder"
import { planningEvent, refinementPlanningScope, verifyPlanningScope } from "./refinement-planning-scope"

export const refinementApprovalReference = z.object({ planning_scope: refinementPlanningScope,
  workflow_id: refinementWorkflow.shape.id, digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict()

export function refinementExecutions(store: Store, parent: Task) {
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()) return []
  const workflows = new RefinementWorkflows(new Refinements(store))
  const runs = new RefinementRuns(workflows.refinements)
  const rows = store.db.query("SELECT id FROM refinement_workflows WHERE json_extract(plan,'$.stages[0].parent_id')=? ORDER BY rowid")
    .all(parent.id) as { id: string }[]
  return rows.map((row) => {
    const saved = workflows.get(row.id)
    return { workflow_id: saved.id, digest: saved.digest, state: saved.state, retry_of: saved.plan.retry_of ?? null,
      recovery_of: saved.plan.recovery_of ?? null,
      controller: "platform-refinement-daemon", stages: saved.plan.stages.map((stage) => {
        const exists = store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id)
        const run = exists ? runs.get(stage.id) : null
        return { task_id: stage.id, engine: stage.engine.name, state: run?.state ?? saved.state,
          job: run?.handle ?? null, planned_job: run?.name ?? null, submitted_at: run?.submitted ?? null,
          error: run?.error ?? null, updated_at: run?.updated ?? null }
      }) }
  })
}

export async function approveRefinementExecution(store: Store, parent: Task,
  context: Parameters<typeof proposeVerifiedRefinementWorkflow>[2], input?: z.infer<typeof refinementApprovalReference>) {
  if (parent.plan.session !== context.sessionID || parent.plan.directory !== context.directory) throw new Error("Refinement approval belongs to another conversation")
  const expected = input ? refinementApprovalReference.parse(input) : undefined
  if (expected) verifyPlanningScope(store, parent, expected.planning_scope)
  const row = planningEvent(store, parent.id, "refinement_workflow_draft", expected?.planning_scope)
  const recorded = row ? JSON.parse(row.payload) : null
  if (recorded?.planning_scope && !expected) throw Error("Workflow-bound refinement requires its exact research approval reference")
  if (expected && (!recorded || recorded.digest !== expected.digest || recorded.workflow?.id !== expected.workflow_id
    || digest(recorded.workflow) !== expected.digest)) throw Error("Bound refinement draft changed or is missing")
  const workflows = new RefinementWorkflows(new Refinements(store))
  const snapshot = refinementDraftSnapshot(store, parent.id, Date.now(), expected?.planning_scope)
  const executions = refinementExecutions(store, parent)
  const replacement = snapshot && 'retry_of' in snapshot && snapshot.state === "COMPILED_NOT_EXECUTABLE" ? snapshot.recovery_of ?? snapshot.retry_of : null
  const existing = executions.filter((row) => row.state !== "CANCELLED" && row.workflow_id !== replacement?.id &&
    !executions.some((next) => next.state !== "CANCELLED" && (next.recovery_of ?? next.retry_of)?.id === row.workflow_id))
  if (existing.length) {
    if (existing.length !== 1 || existing[0].state !== "APPROVED") throw new Error("Refinement confirmation already pending; do not create another workflow")
    if (expected && (existing[0].workflow_id !== expected.workflow_id || existing[0].digest !== expected.digest))
      throw Error("Another refinement task cannot replace the bound research operation")
    workflows.authorize(workflows.get(existing[0].workflow_id).plan.stages[1].id)
    return existing[0] // Lost UI reply must not ask again or duplicate execution.
  }
  if (!snapshot || !('id' in snapshot) || snapshot.state === "EXPIRED" || snapshot.state === "UNAVAILABLE") {
    throw new Error("Generate a fresh platform refinement plan before confirmation")
  }
  const plan = refinementWorkflow.parse(recorded.workflow)
  if (snapshot.digest !== digest(plan)) throw new Error("Refinement draft changed")
  const transport = new Inspire(parent.plan.connection)
  await proposeVerifiedRefinementWorkflow(workflows, plan, context, transport,
    () => admitRefinement(store, plan, context.abort).then(() => undefined))
  return refinementExecutions(store, parent).find((row) => row.workflow_id === plan.id)!
}

export async function runRefinements(store: Store, signal: AbortSignal) {
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()) return
  const workflows = new RefinementWorkflows(new Refinements(store))
  const rows = store.db.query("SELECT id FROM refinement_workflows WHERE state='APPROVED' AND json_extract(plan,'$.stages[0].runtime_admission')='cpu-before-approval-gpu-at-start-v1'")
    .all() as { id: string }[]
  for (const row of rows) {
    if (signal.aborted) return
    const workflow = workflows.get(row.id)
    const runs = new RefinementRuns(workflows.refinements)
    const parent = workflows.refinements.parent(workflow.plan.stages[0])
    const transport = refinementTransport(runs, new Inspire(parent.plan.connection), {
      preflight: () => admitRefinement(store, workflow.plan, signal).then(() => undefined),
      verify: async () => { throw new Error("Legacy outputs are not eligible for production structural QC") },
    })
    await new RefinementRunner(runs, transport).tickWorkflow(workflow.id)
  }
}
