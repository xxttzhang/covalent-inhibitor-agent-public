import z from "zod"
import { digest } from "./model"
import { readReport } from "./report"
import { selection, selectRefinementProposal, readSelectionProposal, selectionBinding } from "./selection-proposal"
import { refinementPlan } from "./refinement-approval"
import { workflowReference, readWorkflowExecution, type WorkflowReference } from "./workflow-execution"
import { Inspire } from "./inspire"
import type { Store } from "./store"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const count = z.number().int().nonnegative().max(20000)
export { selectionBinding } from "./selection-proposal"
const subset = selectionBinding.extend({ source_manifest_digest: hash, input_sha256: hash, identity_sha256: hash,
  status: z.enum(["SUBSET_PROPOSED", "NO_CANDIDATES_MEET_POLICY"]), input_count: count, eligible_count: count,
  excluded_count: count, selected_count: count.max(100), effective_cap: count.max(100),
  candidates: z.array(refinementPlan.shape.input.shape.candidates.element).max(100),
  calibrated_uncertainty: z.literal(false), refinement_approval_required: z.literal(true),
  limitations: z.array(z.string().min(1).max(1000)).min(1).max(20),
}).strict()
export const screeningResult = z.object({ schema: z.literal("library-workflow-screening-result-v1"),
  reference: workflowReference, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/), checked_at: z.number().int().positive(),
  verification: z.enum(["verified", "not_ready", "evidence_rejected", "verification_unavailable", "selection_unavailable"]),
  report_verified_at: z.number().int().positive().nullable(),
  stages: z.array(z.object({ phase: z.enum(["prepare", "carsidock", "rtmscore", "report"]), manifest_digest: hash,
    input_count: count, success_count: count, failed_count: count, artifact_count: z.number().int().positive() }).strict()).max(4),
  selection: subset.nullable(), compute_submitted: z.literal(false), scientific_assessment: z.literal("not_evaluated"),
}).strict()

/** Same native report/selection implementation, scoped by the backend's exact
 * operation. No arbitrary task, latest-event adoption, approval or engine call. */
export async function readWorkflowResults(store: Store, reference: WorkflowReference,
  scope: { directory: string; sessionID: string }, input: {
    selection?: { policy: z.infer<typeof selection>; reason: string }; bound?: z.infer<typeof selectionBinding> | null;
  }, signal: AbortSignal) {
  const execution = readWorkflowExecution(store, reference, scope)
  if (!execution || execution.task_state !== "SUCCEEDED") throw Error("Workflow results require its verified completed native task")
  const task = store.get(execution.task_id)
  const stages = store.stages(task.id)
  const identity = digest({ task, stages })
  const unchanged = () => {
    signal.throwIfAborted()
    if (identity !== digest({ task: store.get(task.id), stages: store.stages(task.id) }))
      throw Error("Native task or evidence changed during result verification")
  }
  const transport = new Inspire(task.plan.connection)
  transport.bound(task)
  unchanged()
  const report = await readReport(task, stages, transport)
  unchanged()
  const base = { schema: "library-workflow-screening-result-v1" as const, reference, task_id: task.id,
    checked_at: Date.now(), report_verified_at: report.verification === "verified" ? report.verified_at : null,
    stages: report.artifacts.map(({ phase, manifest_digest, input_count, success_count, failed_count, artifact_count }) =>
      ({ phase, manifest_digest, input_count, success_count, failed_count, artifact_count })),
    compute_submitted: false as const, scientific_assessment: "not_evaluated" as const }
  const save = (result: z.infer<typeof screeningResult>) => {
    unchanged()
    store.event(task.id, "workflow_screening_result_observed", result)
    return result
  }
  if (report.verification !== "verified")
    return save(screeningResult.parse({ ...base, verification: report.verification, selection: null }))
  const chosen = input.selection
  const bound = input.bound ? selectionBinding.parse(input.bound) : null
  if (!chosen && !bound) return save(screeningResult.parse({ ...base, verification: "verified", selection: null }))
  // Validate caller criteria before remote mutation; failures in an existing
  // witness are reported as unavailable, never replaced with a newer subset.
  const policy = selection.parse(chosen?.policy ?? bound!.policy)
  const reason = selectionBinding.shape.reason.parse(chosen?.reason ?? bound!.reason)
  const result = await (async () => {
    if (chosen) return selectRefinementProposal(store, task, policy, transport, signal)
    return readSelectionProposal(store, task, bound!, transport, signal)
  })().then(value => ({ ok: true as const, value }), () => ({ ok: false as const }))
  unchanged()
  if (!result.ok) return save(screeningResult.parse({ ...base, verification: "selection_unavailable", selection: null }))
  const proof = result.value
  if (!chosen && (proof.selection_id !== bound!.selection_id || proof.selection_manifest_digest !== bound!.selection_manifest_digest))
    return save(screeningResult.parse({ ...base, verification: "evidence_rejected", selection: null }))
  return save(screeningResult.parse({ ...base, verification: "verified", selection: {
    selection_id: proof.selection_id, selection_manifest_digest: proof.selection_manifest_digest,
    source_manifest_digest: proof.source_manifest_digest, policy: proof.policy, reason,
    input_sha256: proof.input.sha256, identity_sha256: proof.input.identity_sha256, status: proof.status,
    input_count: proof.input_count, eligible_count: proof.eligible_count, excluded_count: proof.excluded_count,
    selected_count: proof.selected_count, effective_cap: proof.effective_cap, candidates: proof.input.candidates,
    calibrated_uncertainty: false, refinement_approval_required: true, limitations: proof.limitations,
  } }))
}
