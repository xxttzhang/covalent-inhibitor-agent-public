import z from "zod"
import { digest } from "./model"
import { refinementPlan, proposeRefinement, type RefinementPlan, type Refinements } from "./refinement-approval"
import type { ToolContext } from "@synsci/plugin/tool"
import { readReport } from "./report"
import type { Store } from "./store"
import type { Inspire } from "./inspire"
import { preflightRefinementInterface } from "./engine-interface"
import { refinementCommand } from "./refinement-command"
import { RefinementRuns } from "./refinement-runs"
import { proposeRefinementWorkflow, executableRefinementStages, type RefinementWorkflow, type RefinementWorkflows } from "./refinement-workflow"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const evidence = z.object({ selection_verified: z.literal(true),
  selection_id: hash, source_task_id: z.string(), source_plan_digest: hash, source_manifest_digest: hash,
  selection_manifest_digest: hash, status: z.literal("SUBSET_PROPOSED"),
  input_count: z.number().int().positive().max(20000), eligible_count: z.number().int().positive().max(20000),
  excluded_count: z.number().int().nonnegative().max(20000), selected_count: z.number().int().positive().max(100),
  effective_cap: z.number().int().positive().max(100), artifact_directory: z.string(),
  input: refinementPlan.shape.input, calibrated_uncertainty: z.literal(false),
  gpu_submitted: z.literal(false), refinement_approval_required: z.literal(true),
}).passthrough()

export function verifySelectionEvidence(value: unknown, input: RefinementPlan) {
  const plan = refinementPlan.parse(input)
  const fresh = evidence.parse(value)
  if (fresh.source_task_id !== plan.parent_id || fresh.source_plan_digest !== plan.parent_digest ||
    fresh.source_manifest_digest !== plan.report_digest || fresh.selection_id !== plan.selection_id ||
    fresh.selection_manifest_digest !== plan.selection_manifest_digest) throw new Error("Selection evidence belongs to another frozen plan/report")
  if (digest(fresh.input) !== digest(plan.input)) throw new Error("Selection candidates, reasons or input file differ from refinement approval")
  if (fresh.artifact_directory + "/candidates.parquet" !== plan.input.path ||
    fresh.input_count !== fresh.eligible_count + fresh.excluded_count ||
    fresh.eligible_count !== plan.input.eligible_count || fresh.selected_count !== plan.input.candidates.length ||
    fresh.selected_count > fresh.effective_cap || fresh.effective_cap > Math.floor(fresh.eligible_count * 0.25)) {
    throw new Error("Selection scope, path or denominator mismatch")
  }
  return fresh
}

// Read-only production path through the existing InspireSkill CLI transport.
// Engine/resource/license preflight is a separate mandatory check. This function
// neither proposes an approval nor authorizes, submits or retries any workload.
export async function verifyRefinementSelection(store: Store, value: RefinementPlan,
  transport: Pick<Inspire, "bound" | "verify" | "remote">) {
  const plan = refinementPlan.parse(value)
  const task = store.get(plan.parent_id)
  if (task.digest !== plan.parent_digest || task.plan.directory !== plan.directory || task.plan.session !== plan.session) {
    throw new Error("Refinement source conversation/project changed")
  }
  transport.bound(task)
  const report = await readReport(task, store.stages(task.id), transport)
  if (report.verification !== "verified") throw new Error("Refinement source report not currently verified: " + report.reason)
  const source = report.artifacts.find((item) => item.phase === "report")
  if (source?.manifest_digest !== plan.report_digest) {
    throw new Error("Refinement refers to a different verified report")
  }
  const fresh = verifySelectionEvidence(await transport.remote(task.plan.runtime,
    ["read-selection", task.id, task.digest, plan.selection_id, plan.selection_manifest_digest]), plan)
  if (fresh.input_count !== source.input_count || fresh.eligible_count > source.success_count) {
    throw new Error("Selection denominator differs from freshly verified core results")
  }
  const current = store.get(task.id)
  const stage = store.stages(task.id).find((item) => item.phase === "report")
  if (current.state !== "SUCCEEDED" || current.digest !== task.digest || !stage?.manifest ||
    digest(JSON.parse(stage.manifest)) !== plan.report_digest) throw new Error("Core source changed while verifying selection")
  return fresh
}

// Runtime entry point for future tool registration: the mandatory scientific
// selection check is composed here, not left to a model-provided verifier or an
// optional caller convention. Resource/engine/license preflight remains required.
export async function proposeVerifiedRefinement(store: Refinements, plan: RefinementPlan,
  context: Pick<ToolContext, "sessionID" | "directory" | "abort" | "metadata" | "ask">,
  transport: Pick<Inspire, "bound" | "verify" | "remote" | "live" | "call">, preflight: (plan: RefinementPlan) => Promise<void>) {
  const task = await proposeRefinement(store, plan, context, async (value) => {
    await verifyRefinementSelection(store.store, value, transport)
    context.abort.throwIfAborted()
    refinementCommand(value, store.store.get(value.parent_id)) // Exact executable plan must be constructible before/after approval.
    await preflightRefinementInterface(value, store.store.get(value.parent_id), transport)
    context.abort.throwIfAborted()
    await preflight(value)
  })
  const execution = new RefinementRuns(store).ensure(task.id)
  context.metadata({ title: "已保存独立精筛执行记录", metadata: { task_id: task.id, digest: task.digest,
    state: task.state, execution_state: execution.state, submitted: false } })
  return task
}

// Same production evidence boundary for the single two-engine approval. Fixed
// core/selection/interface checks cannot be replaced by a model's boolean. The
// remaining runtime/license verifier must be provided by the platform adapter.
export async function proposeVerifiedRefinementWorkflow(store: RefinementWorkflows, plan: RefinementWorkflow,
  context: Pick<ToolContext, "sessionID" | "directory" | "abort" | "metadata" | "ask">,
  transport: Pick<Inspire, "bound" | "verify" | "remote" | "live" | "call">, preflight: (plan: RefinementPlan) => Promise<void>) {
  const task = await proposeRefinementWorkflow(store, plan, context, async (value) => {
    await verifyRefinementSelection(store.refinements.store, value, transport)
    context.abort.throwIfAborted()
    refinementCommand(value, store.refinements.store.get(value.parent_id))
    await preflightRefinementInterface(value, store.refinements.store.get(value.parent_id), transport)
    context.abort.throwIfAborted()
    await preflight(value)
  })
  const runs = new RefinementRuns(store.refinements)
  const executions = store.refinements.store.db.transaction(() => executableRefinementStages(task.plan).map((stage) => runs.ensure(stage.id)))()
  context.metadata({ title: task.plan.recovery_of ? "已保存仅恢复 CovDock 的执行记录，AF3 不重跑" : "已保存 AF3 + CovDock 顺序精筛执行记录", metadata: {
    task_id: task.id, digest: task.digest, state: task.state, submitted: false,
    stages: executions.map((run) => ({ task_id: run.id, state: run.state })),
  } })
  return task
}
