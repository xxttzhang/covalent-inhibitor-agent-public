import z from "zod"
import { digest, type Task } from "./model"
import type { Store } from "./store"
import { refinementApprovalReference } from "./refinement-execution"
import { readRefinementObservation } from "./refinement-lifecycle"
import { combineRefinementReport, readRefinementReport } from "./refinement-report"
import { refinementPlan } from "./refinement-approval"
import { refinementAssessment, refinementQualityMeasurements } from "./refinement-quality-policy"
import { RefinementDelivery, refinementDeliveryObservation } from "./refinement-delivery"
import { RefinementWorkflows } from "./refinement-workflow"
import { Refinements } from "./refinement-approval"
import { ensureDelivery } from "./host"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
export const mdCandidateIdentity = z.object({ compound_id: z.string().min(1).max(256).regex(/^[^\x00-\x1f\x7f]+$/),
  input_sdf_index: z.number().int().nonnegative() }).strict()
export const mdCandidateChoice = z.object({ schema: z.literal("library-md-candidate-choice-v1"),
  id: z.string().regex(/^md-choice-[a-f0-9]{32}$/), candidate: mdCandidateIdentity,
  reason: z.string().min(20).max(2000), native_workflow_id: z.string().regex(/^refineflow-[a-f0-9]{24}$/),
  native_workflow_digest: hash, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/), report_digest: hash,
  files: z.object({ candidate_table: refinementDeliveryObservation.shape.files.element,
    covdock_structure: refinementDeliveryObservation.shape.files.element }).strict(),
  parents: z.object({ preparation_run_id: z.string().min(1).max(200), prepared_receptor_manifest_sha256: hash,
    receptor_context: z.object({ id: z.string().regex(/^receptor-context-[a-f0-9]{32}$/), sha256: hash }).strict() }).strict(),
  selected_at: z.number().int().positive(),
}).strict()
const count = z.number().int().min(0).max(100)
const statuses = ["BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED"] as const
const engine = z.object({ status: z.enum(["SUCCESS", "FAILED"]), score: z.number().finite().nullable(),
  quality: z.object({ status: z.enum(["PASSED", "REJECTED", "NOT_EVALUATED"]), reason: z.string().min(1).max(2000).nullable(),
    assessment: refinementAssessment.optional() }).strict(),
  measurements: refinementQualityMeasurements.shape.candidates.element.shape.metrics,
}).strict()
export const workflowRefinementReport = z.object({ report_digest: hash, quality_policy_digest: hash,
  input_identity_sha256: hash, candidate_count: count.min(1),
  candidates: z.array(refinementPlan.shape.input.shape.candidates.element.extend({ status: z.enum(statuses),
    af3: engine, covdock: engine }).strict()).min(1).max(100),
  counts: z.object(Object.fromEntries(statuses.map(status => [status, count])) as Record<typeof statuses[number], typeof count>).strict(),
  stages: z.array(z.object({ engine: z.enum(["af3", "covdock"]), task_id: z.string().regex(/^refine-[a-f0-9]{24}$/),
    plan_digest: hash, outcome_digest: hash, output_manifest_sha256: hash,
    success_count: count, failed_count: count }).strict()).length(2),
  hits_confirmed: z.literal(false), affinity_validated: z.literal(false),
  scope: z.literal("bounded_structural_qc_not_affinity"),
}).strict()
export const workflowRefinementResult = z.object({ schema: z.literal("library-workflow-refinement-result-v1"),
  reference: refinementApprovalReference, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/),
  checked_at: z.number().int().positive(), ledger_sequence: z.number().int().positive(),
  verification: z.enum(["verified", "not_ready", "evidence_rejected", "verification_unavailable"]),
  report: workflowRefinementReport.nullable(), compute_submitted: z.literal(false),
  delivery: refinementDeliveryObservation.nullable().optional(),
  scientific_assessment: z.literal("not_evaluated"),
}).strict()

/** Keep the native classifier's verdicts and measured values. File locations,
 * raw worker errors and approval/source data are not model-facing arguments. */
export function projectRefinementReport(report: ReturnType<typeof combineRefinementReport>) {
  const project = (row: typeof report.candidates[number]["af3"]) => ({ status: row.status, score: row.score,
    quality: { status: row.quality.status, reason: row.quality.reason,
      ...(row.quality.assessment ? { assessment: row.quality.assessment } : {}) }, measurements: row.measurements })
  return workflowRefinementReport.parse({ report_digest: digest(report), quality_policy_digest: digest(report.quality_policy),
    input_identity_sha256: report.input_identity_sha256, candidate_count: report.candidate_count,
    candidates: report.candidates.map(row => ({ ...refinementPlan.shape.input.shape.candidates.element.parse({
      input_sdf_index: row.input_sdf_index, compound_id: row.compound_id, selection_channel: row.selection_channel,
      carsidock_rank_fraction: row.carsidock_rank_fraction, rtmscore_rank_fraction: row.rtmscore_rank_fraction,
      rank_disagreement: row.rank_disagreement }), status: row.status, af3: project(row.af3), covdock: project(row.covdock) })),
    counts: report.counts, stages: report.stages.map(({ engine, task_id, plan_digest, outcome_digest,
      output_manifest_sha256, success_count, failed_count }) => ({ engine, task_id, plan_digest, outcome_digest,
      output_manifest_sha256, success_count, failed_count })),
    hits_confirmed: false, affinity_validated: false, scope: "bounded_structural_qc_not_affinity" })
}

export async function readWorkflowRefinementResults(store: Store, parent: Task,
  reference: z.infer<typeof refinementApprovalReference>, scope: { directory: string; sessionID: string }, signal: AbortSignal,
  publications?: Pick<RefinementDelivery, "inspect">) {
  const before = readRefinementObservation(store, parent, reference)
  const report = await readRefinementReport(store, parent, { ...scope, abort: signal }, reference.workflow_id)
  signal.throwIfAborted()
  const delivery = report.report_ready && report.report && new RefinementWorkflows(new Refinements(store)).get(reference.workflow_id).plan.delivery
    ? await (publications ?? new RefinementDelivery(store)).inspect(reference.workflow_id, parent, signal) : null
  const after = readRefinementObservation(store, store.get(parent.id), reference)
  if (digest({ ...before, observed_at: 0, ledger_sequence: 0 }) !== digest({ ...after, observed_at: 0, ledger_sequence: 0 }))
    throw Error("Bound refinement execution changed during report read")
  if (report.report && (report.report.workflow_id !== reference.workflow_id || report.report.workflow_digest !== reference.digest
    || report.report.parent_task_id !== parent.id || report.report.parent_digest !== parent.digest
    || report.report.selection_id !== reference.planning_scope.selection.selection_id
    || report.report.selection_manifest_digest !== reference.planning_scope.selection.selection_manifest_digest))
    throw Error("Refinement report replaced its bound workflow, parent or subset")
  const value = workflowRefinementResult.parse({ schema: "library-workflow-refinement-result-v1", reference,
    task_id: parent.id, checked_at: Date.now(), ledger_sequence: after.ledger_sequence, verification: report.verification,
    report: report.report ? projectRefinementReport(report.report) : null,
    delivery,
    compute_submitted: false, scientific_assessment: "not_evaluated" })
  return { result: value, observation: after }
}

/** Resume only the bound, independently approved file queue. The existing
 * delivery observer owns transfers; this path cannot start a compute daemon. */
export async function resumeWorkflowRefinementDelivery(store: Store, parent: Task,
  reference: z.infer<typeof refinementApprovalReference>, scope: { directory: string; sessionID: string },
  signal: AbortSignal, observer: typeof ensureDelivery = ensureDelivery) {
  signal.throwIfAborted()
  if (parent.plan.directory !== scope.directory || parent.plan.session !== scope.sessionID)
    throw Error("Refinement delivery belongs to another conversation")
  const queue = new RefinementDelivery(store)
  const resume = store.db.transaction(() => {
    const observed = readRefinementObservation(store, store.get(parent.id), reference)
    if (observed.execution_state !== "completed" || !observed.approval_recorded)
      throw Error("Refinement delivery recovery requires both approved completed executions")
    const bound = queue.scope(reference.workflow_id)
    if (bound.workflow.digest !== reference.digest || bound.task.digest !== parent.digest)
      throw Error("Refinement delivery recovery identity changed")
    const row = queue.get(reference.workflow_id)
    if (row && row.digest !== reference.digest) throw Error("Refinement delivery queue belongs to another plan")
    if (row?.state === "COMPLETE") return false
    queue.resume(reference.workflow_id, parent)
    return true
  }).immediate()
  if (!resume) return false
  signal.throwIfAborted()
  await observer(store)
  return true
}
