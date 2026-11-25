import z from "zod"
import { digest, type Task } from "./model"
import type { Store } from "./store"
import type { BoundRequest } from "./request"
import { planRefinement, refinementProposalSnapshot } from "./refinement-proposal"
import { prepareRefinementDraft, refinementDraftSnapshot } from "./refinement-builder"
import { refinementPlanningScope, verifyPlanningScope, type RefinementPlanningScope } from "./refinement-planning-scope"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
export const refinementPlanningResult = z.object({ schema: z.literal("library-workflow-refinement-planning-v1"),
  planning_scope: refinementPlanningScope, task_id: z.string().regex(/^screen-[a-f0-9]{24}$/),
  checked_at: z.number().int().positive(),
  proposal: z.object({ digest: hash, state: z.enum(["BLOCKED", "PENDING_RUNTIME_CONFIRMATION"]),
    proposed_points: z.number().finite().nonnegative(), selected_count: z.number().int().min(1).max(100),
    quality_policy_digest: hash, blockers: z.array(z.string()).max(30),
    stages: z.array(z.object({ engine: z.enum(["af3", "covdock"]), minutes: z.number().int().positive(),
      candidate_count: z.number().int().min(1).max(100), proposed_points: z.number().finite().nonnegative() }).strict()).length(2),
  }).strict().nullable(),
  draft: z.object({ workflow_id: z.string().regex(/^refineflow-[a-f0-9]{24}$/), digest: hash, proposal_digest: hash,
    state: z.enum(["COMPILED_NOT_EXECUTABLE", "EXPIRED"]), approve_before: z.number().int().positive(),
    blockers: z.array(z.string()).max(30) }).strict().nullable(),
  compute_submitted: z.literal(false), approval_recorded: z.literal(false), scientific_assessment: z.literal("not_evaluated"),
}).strict()

/** Compact receipt only. The original proposal/compiler perform all scientific,
 * file, environment and interface checks; no local path or command is exposed. */
export function refinementPlanningObservation(store: Store, task: Task, input: RefinementPlanningScope, now = Date.now()) {
  const scope = verifyPlanningScope(store, task, input)
  const proposal = refinementProposalSnapshot(store, task, scope)?.proposal
  const draft = refinementDraftSnapshot(store, task.id, now, scope)
  return refinementPlanningResult.parse({ schema: "library-workflow-refinement-planning-v1", planning_scope: scope,
    task_id: task.id, checked_at: now, proposal: proposal ? { digest: digest(proposal), state: proposal.state,
      proposed_points: proposal.proposed_points, selected_count: proposal.selection.selected_count,
      quality_policy_digest: proposal.quality_policy.policy_digest, blockers: proposal.blockers,
      stages: proposal.stages.map(stage => ({ engine: stage.engine, minutes: stage.minutes,
        candidate_count: stage.candidate_count, proposed_points: stage.proposed_points })) } : null,
    draft: draft && "id" in draft ? { workflow_id: draft.id, digest: draft.digest, proposal_digest: draft.proposal_digest,
      state: draft.state, approve_before: draft.approve_before, blockers: draft.blockers } : null,
    compute_submitted: false, approval_recorded: false, scientific_assessment: "not_evaluated" })
}

export async function prepareWorkflowRefinement(store: Store, task: Task, input: RefinementPlanningScope,
  source: BoundRequest, signal: AbortSignal) {
  signal.throwIfAborted()
  const scope = verifyPlanningScope(store, task, input)
  const proposed = await planRefinement(store, task, signal, scope)
  signal.throwIfAborted()
  if (proposed?.proposal?.state === "PENDING_RUNTIME_CONFIRMATION")
    await prepareRefinementDraft(store, task.id, source, scope)
  signal.throwIfAborted()
  return refinementPlanningObservation(store, store.get(task.id), scope)
}
