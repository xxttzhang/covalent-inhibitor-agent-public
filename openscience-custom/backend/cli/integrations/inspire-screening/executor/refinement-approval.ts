import z from "zod"
import { randomBytes } from "node:crypto"
import type { ToolContext } from "@synsci/plugin/tool"
import { digest, resource, verifyOutcome } from "./model"
import { origin, requestHash } from "./request"
import type { Store } from "./store"
import { refinementRuntime, refinementPaths } from "./refinement-runtime"
import { refinementQualityPolicy } from "./refinement-quality-policy"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const candidate = z.object({
  input_sdf_index: z.number().int().nonnegative(),
  compound_id: z.string().min(1).max(256).refine((value) => value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)),
  selection_channel: z.enum(["rank_disagreement_review", "consensus_sentinel", "diversity_sentinel"]),
  carsidock_rank_fraction: z.number().min(0).max(1), rtmscore_rank_fraction: z.number().min(0).max(1),
  rank_disagreement: z.number().min(0).max(1),
}).strict()

// One frozen engine invocation: either independently approved or explicitly
// included in a two-engine workflow approval. Never inherit the other engine's
// or the core task's grant. Adapters must supply exact files and resource facts.
export const refinementPlan = z.object({
  version: z.literal("screening-refinement-v1"), id: z.string().regex(/^refine-[a-f0-9]{24}$/),
  request: z.string().min(1).max(10000), origin,
  directory: z.string().startsWith("/"), session: z.string().min(1),
  parent_id: z.string().regex(/^screen-[a-f0-9]{24}$/), parent_digest: hash, report_digest: hash,
  selection_id: hash, selection_manifest_digest: hash,
  // Opt-in and digest-bound: old approvals keep their historical result meaning.
  result_contract: z.literal("screening-refinement-outcome-v2").optional(),
  geometry_contract: z.literal("af3-geometry-evidence-v1").optional(),
  quality_policy: refinementQualityPolicy.optional(),
  runtime_admission: z.literal("cpu-before-approval-gpu-at-start-v1").optional(),
  environment_contract: z.literal("no-loader-injection-v1").optional(),
  code: z.object({ release_id: hash, manifest_sha256: hash }).strict(),
  runtime: refinementRuntime,
  input: z.object({ path: z.string().startsWith("/inspire/"), sha256: hash, identity_sha256: hash,
    eligible_count: z.number().int().min(4).max(20000), candidates: z.array(candidate).min(1).max(100),
    calibrated_uncertainty: z.literal(false) }).strict(),
  engine: z.discriminatedUnion("name", [
    z.object({ name: z.literal("af3"), num_recycles: z.number().int().min(1).max(20),
      num_seeds: z.number().int().min(1).max(20), num_diffusion_samples: z.number().int().min(1).max(20),
      run_data_pipeline: z.boolean(), cpu_per_search: z.number().int().positive(),
      random_seed: z.number().int().nonnegative(), min_coverage: z.number().min(0.95).max(1) }).strict(),
    z.object({ name: z.literal("covdock"), mode: z.literal("LO"), reaction: z.literal("Michael Addition"),
      input_score_source: z.literal("core"),
      max_structures_per_compound: z.literal(1), min_coverage: z.number().min(0.95).max(1) }).strict(),
  ]),
  // Paths/hashes cover entry point, supporting source, environment, receptor and
  // model/database or license configuration. Existence alone is not preflight.
  assets: z.record(z.string().startsWith("/inspire/"), hash).refine((value) => Object.keys(value).length >= 5),
  resource, minutes: z.number().int().min(1).max(10080), max_points: z.number().nonnegative(),
  created: z.number().int().nonnegative(), approve_before: z.number().int().positive(),
  finish_before: z.number().int().positive(),
}).strict().superRefine((value, ctx) => {
  const fail = (message: string) => ctx.addIssue({ code: "custom", message })
  if (value.origin.request_sha256 !== requestHash(value.request)) fail("Refinement request differs from original user message")
  const rows = value.input.candidates
  if (rows.length > Math.floor(value.input.eligible_count * 0.25)) fail("Refinement cannot exceed 25% of eligible core candidates")
  if (new Set(rows.map((row) => row.input_sdf_index)).size !== rows.length || new Set(rows.map((row) => row.compound_id)).size !== rows.length) fail("Duplicate refinement identity")
  const identities = rows.map((row) => [row.input_sdf_index, row.compound_id]).sort((a, b) => Number(a[0]) - Number(b[0]))
  if (digest(identities) !== value.input.identity_sha256) fail("Refinement identity digest differs from displayed list")
  if (rows.some((row) => Math.abs(Math.abs(row.carsidock_rank_fraction - row.rtmscore_rank_fraction) - row.rank_disagreement) > 1e-10)) fail("Displayed rank disagreement is inconsistent")
  if (value.engine.name === "af3" && value.resource.kind !== "job") fail("AF3 requires an explicit GPU Job")
  if (value.engine.name === "covdock" && value.resource.kind !== "hpc") fail("CovDock requires an explicit CPU HPC resource")
  if (value.runtime.name !== value.engine.name) fail("Runtime asset roles differ from the selected engine")
  if (value.geometry_contract && value.engine.name !== "af3") fail("AF3 geometry contract cannot be applied to another engine")
  if (value.quality_policy && (value.result_contract !== "screening-refinement-outcome-v2" ||
    value.engine.name === "af3" && !value.geometry_contract)) fail("Structural QC requires classified outcomes and actual AF3 geometry readback")
  if (value.engine.name === "af3" && value.engine.cpu_per_search > Number(value.resource.quota.split(",")[1])) fail("AF3 search CPUs exceed approved quota")
  if (value.minutes / 60 * value.resource.points_per_hour > value.max_points + 1e-8) fail("Refinement resource reservation exceeds its independent points cap")
  if (value.approve_before <= value.created || value.finish_before <= value.approve_before) fail("Invalid refinement deadlines")
})
export type RefinementPlan = z.infer<typeof refinementPlan>
type Draft = { id: string; plan: RefinementPlan; digest: string; state: "DRAFT" | "APPROVED" | "WORKFLOW_APPROVED" | "CANCELLED"; approval: string | null }
type Context = Pick<ToolContext, "sessionID" | "directory" | "abort" | "metadata" | "ask">

export class Refinements {
  constructor(readonly store: Store) {
    store.db.exec(`CREATE TABLE IF NOT EXISTS refinements(id TEXT PRIMARY KEY, plan TEXT NOT NULL,
      digest TEXT NOT NULL, state TEXT NOT NULL, approval TEXT)`)
  }
  parent(plan: RefinementPlan) {
    const task = this.store.get(plan.parent_id)
    const stages = this.store.stages(task.id)
    if (task.state !== "SUCCEEDED" || task.digest !== plan.parent_digest || task.plan.directory !== plan.directory ||
      task.plan.session !== plan.session || !task.approval || JSON.parse(task.approval).digest !== task.digest ||
      stages.length !== 4 || stages.some((stage) => stage.state !== "SUCCEEDED" || !stage.manifest)) throw new Error("Refinement requires this conversation's approved and verified core task")
    const chain = stages.reduce<ReturnType<typeof verifyOutcome>[]>((saved, stage, ordinal) => {
      if (stage.task_id !== task.id || stage.ordinal !== ordinal || stage.phase !== task.plan.steps[ordinal].phase) throw new Error("Core stage identity changed")
      return [...saved, verifyOutcome(JSON.parse(stage.manifest!), task, stage, saved[ordinal - 1] ?? null)]
    }, [])
    if (digest(chain[3]) !== plan.report_digest) throw new Error("Refinement refers to a different core report")
    const expected = `${task.plan.runtime.root}/tasks/${task.id}/selections/${plan.selection_id}/candidates.parquet`
    if (plan.input.path !== expected) throw new Error("Refinement input must be the frozen selection artifact, not a historical pool")
    if (plan.input.eligible_count > chain[3].success_count) throw new Error("Selection denominator exceeds verified core successes")
    if (refinementPaths(plan.runtime, task.plan.runtime.root, plan.code.release_id).files.some((file) => !plan.assets[file])) {
      throw new Error("Refinement runtime file roles are absent from frozen assets")
    }
    return task
  }
  create(value: RefinementPlan) {
    const plan = refinementPlan.parse(value)
    return this.store.db.transaction(() => {
      this.parent(plan)
      this.store.db.query("INSERT INTO refinements(id,plan,digest,state) VALUES(?,?,?,'DRAFT')").run(plan.id, JSON.stringify(plan), digest(plan))
      this.store.event(plan.parent_id, "refinement_draft_created", { id: plan.id, digest: digest(plan) })
      return this.get(plan.id)
    })()
  }
  get(id: string): Draft {
    const row = this.store.db.query("SELECT * FROM refinements WHERE id=?").get(id) as (Omit<Draft, "plan"> & { plan: string }) | null
    if (!row) throw new Error("Unknown refinement task")
    const plan = refinementPlan.parse(JSON.parse(row.plan))
    if (plan.id !== row.id || digest(plan) !== row.digest) throw new Error("Immutable refinement plan changed")
    return { ...row, plan }
  }
  workflow(id: string) {
    if (!this.store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_workflow_members'").get()) return null
    return this.store.db.query("SELECT workflow_id FROM refinement_workflow_members WHERE refinement_id=?").get(id) as { workflow_id: string } | null
  }
  cancel(id: string, directory: string) {
    if (this.workflow(id)) throw new Error("Workflow members must be cancelled through their workflow, not independently")
    const task = this.get(id)
    if (task.plan.directory !== directory || task.state !== "DRAFT") throw new Error("Only this project's unapproved refinement draft can be cancelled here")
    this.store.db.query("UPDATE refinements SET state='CANCELLED' WHERE id=? AND state='DRAFT'").run(id)
    this.store.event(task.plan.parent_id, "refinement_draft_cancelled", { id })
  }
  endApproval(id: string, expected: string, context: Pick<Context, "directory" | "sessionID">, reason: string) {
    return this.store.db.transaction(() => {
      if (this.workflow(id)) throw new Error("Workflow members require workflow-level approval cleanup")
      const task = this.get(id)
      if (task.digest !== expected || task.plan.session !== context.sessionID || task.plan.directory !== context.directory) {
        throw new Error("Interrupted refinement approval belongs to another plan/conversation/project")
      }
      // An interrupted caller must not revoke another completed approval or
      // label an existing execution safely cancelled without observing it.
      if (task.approval || task.state !== "DRAFT") return task
      const executions = this.store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_runs'").get()
      if (executions && this.store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(id)) {
        throw new Error("Unapproved refinement unexpectedly has an execution; review required")
      }
      this.store.db.query("UPDATE refinements SET state='CANCELLED' WHERE id=? AND state='DRAFT' AND approval IS NULL").run(id)
      this.store.event(task.plan.parent_id, "refinement_approval_ended_without_grant", { id, digest: expected, reason })
      return this.get(id)
    })()
  }
  approve(id: string, expected: string, context: Context) {
    return this.store.db.transaction(() => {
      if (this.workflow(id)) throw new Error("Workflow members require the single exact-workflow approval")
      const task = this.get(id)
      if (task.state !== "DRAFT" || task.digest !== expected || task.plan.session !== context.sessionID || task.plan.directory !== context.directory) throw new Error("Refinement approval identity/state changed")
      if (Date.now() > task.plan.approve_before) throw new Error("Refinement approval expired")
      context.abort.throwIfAborted()
      this.parent(task.plan)
      const receipt = { digest: expected, session: context.sessionID, at: Date.now(), boundary: "remote_compute", nonce: randomBytes(16).toString("hex") }
      this.store.db.query("UPDATE refinements SET state='APPROVED',approval=? WHERE id=? AND state='DRAFT'").run(JSON.stringify(receipt), id)
      this.store.event(task.plan.parent_id, "refinement_approved", { id, ...receipt })
      return this.get(id)
    })()
  }
}

// Not a model-callable tool yet: the runtime adapter must supply a mandatory
// fresh verifier of core files, selected list/reasons/hash, engine assets and live
// quota/license preflight. No default, boolean approval or inherited core receipt.
export async function proposeRefinement(store: Refinements, value: RefinementPlan, context: Context,
  verify: (plan: RefinementPlan) => Promise<void>) {
  const plan = refinementPlan.parse(value)
  if (plan.session !== context.sessionID || plan.directory !== context.directory) throw new Error("Refinement proposal belongs to another conversation/project")
  context.abort.throwIfAborted()
  store.parent(plan)
  if (Date.now() > plan.approve_before) throw new Error("Refinement proposal already expired")
  await verify(plan)
  context.abort.throwIfAborted()
  if (Date.now() > plan.approve_before) throw new Error("Refinement proposal expired during verification")
  const task = store.create(plan)
  try {
    context.metadata({ title: "等待独立精筛资源审批", metadata: { task_id: task.id, digest: task.digest, state: "DRAFT" } })
    store.store.event(plan.parent_id, "refinement_approval_requested", { id: task.id, digest: task.digest })
    await context.ask({ permission: "remote_compute", patterns: [task.digest], always: [], metadata: { compute: {
      provider: "inspire", task_id: task.id, digest: task.digest, parent_task_id: plan.parent_id,
      purpose: `独立 ${plan.engine.name.toUpperCase()} 精筛`, project: plan.resource.project,
      account: store.parent(plan).plan.connection.account, notebook: store.parent(plan).plan.connection.notebook,
      candidates: plan.input.candidates.length, minutes: plan.minutes, maximum_compute_points: plan.max_points,
      stages: [{ phase: plan.engine.name, minutes: plan.minutes, resource: plan.resource }],
      structure_site: "EGFR C797（受体和配置资产已冻结）", selection_id: plan.selection_id,
      selected_candidates: plan.input.candidates, selection_manifest_digest: plan.selection_manifest_digest,
      input_sha256: plan.input.sha256, identity_sha256: plan.input.identity_sha256,
      engine_parameters: plan.engine, approve_before: plan.approve_before,
      result_contract: plan.result_contract ?? "screening-refinement-outcome-v1",
      scientific_quality_policy: plan.quality_policy ?? null,
      code_release: plan.code,
      runtime_assets: plan.runtime,
      warning: "这是独立新增预算，仅批准所列候选与单个引擎。排名分歧是复核线索，不是校准的不确定性或活性预测；对照候选单独标注。失败不自动重试，不能扩充名单或继承核心筛选审批。",
    } } })
    context.abort.throwIfAborted()
    const current = store.get(task.id)
    if (current.state !== "DRAFT" || current.digest !== task.digest || Date.now() > current.plan.approve_before) throw new Error("Refinement draft changed or expired while awaiting approval")
    await verify(current.plan) // Files/resources may have changed during the human wait.
    return store.approve(task.id, task.digest, context)
  } catch (error) {
    const reason = context.abort.aborted
      ? "精筛审批已中断；未批准新增计算。此草稿已关闭，不会自动重新申请。"
      : Date.now() > plan.approve_before
        ? "精筛审批已过期；未批准新增计算。需要重新确认候选范围与预算。"
        : "未获得有效精筛批准或审批后复核未通过；此草稿已关闭。请查看原始错误或反馈。"
    try {
      const current = store.endApproval(task.id, task.digest, context, reason)
      if (current.state === "CANCELLED" && !current.approval)
        context.metadata({ title: "精筛审批未完成，草稿已关闭", metadata: {
          task_id: task.id, digest: task.digest, state: current.state, reason,
        } })
    } catch {
      // Do not repair corrupted plans or replace the original permission error.
      store.store.event(plan.parent_id, "refinement_approval_cleanup_requires_review", { id: task.id, digest: task.digest })
    }
    throw error
  }
}
