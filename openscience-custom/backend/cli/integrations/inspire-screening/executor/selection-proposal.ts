import z from "zod"
import { digest, verifyOutcome, type Task, type Stage } from "./model"
import { refinementPlan } from "./refinement-approval"
import type { Store } from "./store"
import type { Inspire } from "./inspire"
import { readReport } from "./report"

export const selection = z.object({
  max_candidates: z.number().int().min(1).max(100), max_fraction: z.number().positive().max(0.25),
  disagreement_threshold: z.number().positive().max(1), top_fraction: z.number().positive().max(1),
  uncertainty_count: z.number().int().min(1).max(100), consensus_count: z.number().int().min(0).max(100),
  diversity_count: z.number().int().min(0).max(100),
}).strict().refine((value) => value.uncertainty_count + value.consensus_count + value.diversity_count <= value.max_candidates,
  "Selection channel quotas exceed the total cap")

const hash = z.string().regex(/^[a-f0-9]{64}$/)
export const selectionBinding = z.object({ selection_id: hash, selection_manifest_digest: hash,
  policy: selection, reason: z.string().trim().min(20).max(2000) }).strict()
const count = z.number().int().nonnegative().max(20000)
// Unlike an executable refinement input, a selection proposal may legitimately
// contain zero candidates. Never turn that result into an implicit fallback.
const proposal = z.object({
  selection_verified: z.literal(true), selection_id: hash, source_task_id: z.string(),
  source_plan_digest: hash, source_manifest_digest: hash, selection_manifest_digest: hash,
  status: z.enum(["SUBSET_PROPOSED", "NO_CANDIDATES_MEET_POLICY"]), policy: selection,
  input_count: count, eligible_count: count, excluded_count: count, selected_count: count.max(100),
  effective_cap: count.max(100), artifact_directory: z.string(),
  input: z.object({ path: z.string(), sha256: hash, identity_sha256: hash, eligible_count: count,
    candidates: z.array(refinementPlan.shape.input.shape.candidates.element).max(100),
    calibrated_uncertainty: z.literal(false) }).strict(),
  calibrated_uncertainty: z.literal(false), gpu_submitted: z.literal(false), refinement_approval_required: z.literal(true),
  limitations: z.array(z.string().min(1).max(1000)).min(1).max(20),
}).strict()

export function verifySelectionProposal(value: unknown, task: Task, stages: Stage[], policy: z.infer<typeof selection>) {
  const chosen = selection.parse(policy)
  const result = proposal.parse(value)
  if (task.state !== "SUCCEEDED" || digest(task.plan) !== task.digest || !task.approval ||
    JSON.parse(task.approval).digest !== task.digest || stages.length !== 4) throw new Error("Selection requires a verified approved core task")
  const chain = stages.reduce<ReturnType<typeof verifyOutcome>[]>((saved, stage, index) => {
    if (stage.task_id !== task.id || stage.ordinal !== index || stage.phase !== task.plan.steps[index].phase ||
      stage.state !== "SUCCEEDED" || !stage.manifest) throw new Error("Selection source stage chain changed")
    return [...saved, verifyOutcome(JSON.parse(stage.manifest), task, stage, saved[index - 1] ?? null)]
  }, [])
  const report = chain[3]
  if (result.source_task_id !== task.id || result.source_plan_digest !== task.digest ||
    result.source_manifest_digest !== digest(report) || digest(result.policy) !== digest(chosen)) {
    throw new Error("Selection belongs to another task, report or policy")
  }
  const directory = `${task.plan.runtime.root}/tasks/${task.id}/selections/${result.selection_id}`
  if (result.artifact_directory !== directory || result.input.path !== `${directory}/candidates.parquet`) {
    throw new Error("Selection path is not the exact current task subset")
  }
  const rows = result.input.candidates
  const identities = rows.map((row) => [row.input_sdf_index, row.compound_id]).sort((a, b) => Number(a[0]) - Number(b[0]))
  if (new Set(rows.map((row) => row.input_sdf_index)).size !== rows.length ||
    new Set(rows.map((row) => row.compound_id)).size !== rows.length || digest(identities) !== result.input.identity_sha256) {
    throw new Error("Selection candidate identity digest differs or contains duplicates")
  }
  if (result.input_count !== report.input_count || result.input_count !== result.eligible_count + result.excluded_count ||
    result.eligible_count > report.success_count || result.input.eligible_count !== result.eligible_count ||
    result.selected_count !== rows.length || result.effective_cap !== Math.min(chosen.max_candidates, Math.floor(result.eligible_count * chosen.max_fraction)) ||
    rows.length > result.effective_cap || (result.status === "SUBSET_PROPOSED") !== (rows.length > 0)) {
    throw new Error("Selection counts, effective cap or empty-result status disagree")
  }
  const channels = { rank_disagreement_review: chosen.uncertainty_count,
    consensus_sentinel: chosen.consensus_count, diversity_sentinel: chosen.diversity_count }
  for (const [channel, cap] of Object.entries(channels)) {
    if (rows.filter((row) => row.selection_channel === channel).length > cap) throw new Error("Selection exceeded its channel budget")
  }
  if (rows.some((row) => Math.abs(Math.abs(row.carsidock_rank_fraction - row.rtmscore_rank_fraction) - row.rank_disagreement) > 1e-10 ||
    Math.min(row.carsidock_rank_fraction, row.rtmscore_rank_fraction) > chosen.top_fraction ||
    (row.selection_channel === "rank_disagreement_review" && row.rank_disagreement < chosen.disagreement_threshold) ||
    (row.selection_channel === "consensus_sentinel" && Math.max(row.carsidock_rank_fraction, row.rtmscore_rank_fraction) > chosen.top_fraction))) {
    throw new Error("Selection reason is inconsistent with its ranks or explicit policy")
  }
  return result
}

export async function selectRefinementProposal(store: Store, task: Task, policy: z.infer<typeof selection>,
  transport: Pick<Inspire, "bound" | "verify" | "remote">, signal: AbortSignal) {
  const chosen = selection.parse(policy)
  signal.throwIfAborted()
  transport.bound(task)
  const stages = store.stages(task.id)
  const report = await readReport(task, stages, transport)
  if (report.verification !== "verified") throw new Error("Selection source report not currently verified: " + report.reason)
  signal.throwIfAborted()
  const result = verifySelectionProposal(await transport.remote(task.plan.runtime, ["select-refinement", task.id, task.digest,
    Buffer.from(JSON.stringify(chosen)).toString("base64")]), task, stages, chosen)
  signal.throwIfAborted()
  // Retain only a result independently re-read from its exact immutable witness.
  const fresh = verifySelectionProposal(await transport.remote(task.plan.runtime,
    ["read-selection", task.id, task.digest, result.selection_id, result.selection_manifest_digest]),
    store.get(task.id), store.stages(task.id), chosen)
  signal.throwIfAborted()
  if (digest(fresh) !== digest(result)) throw new Error("Selection evidence changed between creation and readback")
  store.event(task.id, "refinement_subset_proposed", fresh)
  return fresh
}

/** Read an explicit immutable witness. No selection, repair, latest-event
 * fallback or resource operation is permitted through this path. */
export async function readSelectionProposal(store: Store, task: Task, input: z.infer<typeof selectionBinding>,
  transport: Pick<Inspire, "bound" | "verify" | "remote">, signal: AbortSignal) {
  const bound = selectionBinding.parse(input)
  signal.throwIfAborted()
  transport.bound(task)
  const identity = digest({ task, stages: store.stages(task.id) })
  const report = await readReport(task, store.stages(task.id), transport)
  if (report.verification !== "verified") throw Error("Bound selection source report not currently verified")
  signal.throwIfAborted()
  const result = verifySelectionProposal(await transport.remote(task.plan.runtime,
    ["read-selection", task.id, task.digest, bound.selection_id, bound.selection_manifest_digest]), task, store.stages(task.id), bound.policy)
  signal.throwIfAborted()
  if (identity !== digest({ task: store.get(task.id), stages: store.stages(task.id) })
    || result.selection_id !== bound.selection_id || result.selection_manifest_digest !== bound.selection_manifest_digest)
    throw Error("Bound selection or its native parent changed")
  return result
}

// Presentation of persisted evidence only: no remote read, selection, engine or
// approval. An invalid latest record is reported, never replaced by an older one.
export function selectionSnapshot(store: Store, task: Task) {
  const row = store.db.query("SELECT at,payload FROM events WHERE task_id=? AND kind='refinement_subset_proposed' ORDER BY sequence DESC LIMIT 1")
    .get(task.id) as { at: number; payload: string } | null
  if (!row) return null
  const base = { version: 1 as const, task_id: task.id, plan_digest: task.digest, recorded_at: row.at,
    grants_resource_approval: false as const, additional_budget_points: null, proposal_submitted_compute: false as const }
  try {
    const input = proposal.parse(JSON.parse(row.payload))
    const saved = verifySelectionProposal(input, task, store.stages(task.id), input.policy)
    return { ...base, state: saved.status, selection_id: saved.selection_id,
      selection_manifest_digest: saved.selection_manifest_digest, input_count: saved.input_count,
      eligible_count: saved.eligible_count, excluded_count: saved.excluded_count,
      selected_count: saved.selected_count, effective_cap: saved.effective_cap,
      policy: saved.policy, candidates: saved.input.candidates, calibrated_uncertainty: false as const,
      note: "已保存的候选提案，不是当前远端文件可用性证明。排名分歧是未校准的复核线索，对照不是不确定候选；精筛须另行确定预算并审批。" }
  } catch {
    return { ...base, state: "UNAVAILABLE" as const,
      reason: "最新候选提案与保存的核心证据不一致或格式无效；未展示旧名单替代它，请先检查任务证据。" }
  }
}
