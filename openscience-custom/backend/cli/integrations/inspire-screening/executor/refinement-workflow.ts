import z from "zod"
import { assertRefinementDraftOpen } from "./refinement-planning-scope"
import { randomBytes } from "node:crypto"
import type { ToolContext } from "@synsci/plugin/tool"
import { digest } from "./model"
import { Refinements, refinementPlan, type RefinementPlan } from "./refinement-approval"
import { verifyRefinementResult } from "./refinement-result"
import { refinementDeliveryPolicy } from "./refinement-delivery-policy"

// One explicit approval covers two immutable engine plans. CovDock evaluates the
// same selected molecules independently; it does NOT inherit AF3 poses or silently
// discard structural rejections. An alternative dependency needs a new contract.
export const refinementWorkflow = z.object({
  version: z.literal("screening-refinement-workflow-v1"),
  id: z.string().regex(/^refineflow-[a-f0-9]{24}$/),
  stages: z.tuple([refinementPlan, refinementPlan]),
  execution: z.literal("af3_then_covdock_same_subset"),
  failure_policy: z.literal("stop_on_execution_failure"),
  quality_policy: z.literal("retain_rejections_and_continue"),
  max_points: z.number().finite().nonnegative(),
  delivery: refinementDeliveryPolicy.optional(),
  retry_of: z.object({ id: z.string().regex(/^refineflow-[a-f0-9]{24}$/),
    digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict().optional(),
  recovery_of: z.object({ id: z.string().regex(/^refineflow-[a-f0-9]{24}$/),
    digest: z.string().regex(/^[a-f0-9]{64}$/),
    af3_result_digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict().optional(),
}).strict().superRefine((plan, ctx) => {
  const fail = (message: string) => ctx.addIssue({ code: "custom", message })
  const [af3, covdock] = plan.stages
  if (plan.retry_of && plan.recovery_of) fail("Recovery and full retry are mutually exclusive")
  if (af3.engine.name !== "af3" || covdock.engine.name !== "covdock" || af3.id === covdock.id) fail("Workflow requires distinct AF3 then CovDock plans")
  for (const key of ["request", "origin", "directory", "session", "parent_id", "parent_digest", "report_digest",
    "selection_id", "selection_manifest_digest", "input", "code", "created", "approve_before", "finish_before", "quality_policy", "runtime_admission"] as const) {
    if (plan.recovery_of && ["request", "origin", "code", "created", "approve_before", "finish_before", "runtime_admission"].includes(key)) continue
    if (digest(af3[key] ?? null) !== digest(covdock[key] ?? null)) fail(`Workflow stages differ in frozen ${key}`)
  }
  if (plan.stages.some((stage) => stage.result_contract !== "screening-refinement-outcome-v2")) fail("Both engines must distinguish execution failures from scientific rejections")
  if (Math.abs((plan.recovery_of ? 0 : af3.max_points) + covdock.max_points - plan.max_points) > 1e-8) fail("Workflow budget must equal the new execution reservations")
  const current = plan.recovery_of ? covdock : af3
  if (current.finish_before - current.approve_before < ((plan.recovery_of ? 0 : af3.minutes) + covdock.minutes) * 60000) fail("Workflow deadline cannot fit approved stage runtimes")
  for (const [file, hash] of Object.entries(af3.assets)) {
    if (!plan.recovery_of && covdock.assets[file] && covdock.assets[file] !== hash) fail("Workflow stages disagree on a shared asset hash")
  }
})
export type RefinementWorkflow = z.infer<typeof refinementWorkflow>
export function executableRefinementStages(plan: RefinementWorkflow) {
  return plan.recovery_of ? [plan.stages[1]] : plan.stages
}
type Context = Pick<ToolContext, "sessionID" | "directory" | "abort" | "metadata" | "ask">
type Workflow = { id: string; digest: string; plan: RefinementWorkflow; state: "DRAFT" | "APPROVED" | "CANCELLED"; approval: string | null }
const receipt = z.object({
  digest: z.string().regex(/^[a-f0-9]{64}$/), session: z.string().min(1),
  at: z.number().int().nonnegative(), boundary: z.literal("remote_compute"),
  scope: z.literal("screening-refinement-workflow-v1"), nonce: z.string().regex(/^[a-f0-9]{32}$/),
}).strict()

export class RefinementWorkflows {
  constructor(readonly refinements: Refinements) {
    refinements.store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_workflows(
      id TEXT PRIMARY KEY, plan TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, approval TEXT);
      CREATE TABLE IF NOT EXISTS refinement_workflow_members(
      workflow_id TEXT NOT NULL, refinement_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, digest TEXT NOT NULL,
      UNIQUE(workflow_id, ordinal))`)
  }
  failedBeforeCovdock(id: string) {
    const previous = this.get(id)
    if (previous.state !== "APPROVED" || !this.refinements.store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()) return false
    this.authorize(previous.plan.stages[0].id)
    const runs = previous.plan.stages.map((stage) => this.refinements.store.db.query(
      "SELECT plan_digest,state,submitted,handle FROM refinement_runs WHERE id=?").get(stage.id) as
      { plan_digest: string; state: string; submitted: number | null; handle: string | null } | null)
    if (runs.some((run, index) => !run || run.plan_digest !== digest(previous.plan.stages[index]))) return false
    return runs[0]!.state === "FAILED" && runs[0]!.submitted !== null && !!runs[0]!.handle &&
      runs[1]!.state === "CANCELLED" && runs[1]!.submitted === null && runs[1]!.handle === null
  }
  planningSource(parent: string) {
    // A cancelled, never-approved confirmation must not hide a completed AF3
    // ancestor. Active drafts and granted workflows still prevent duplicates.
    const row = this.refinements.store.db.query("SELECT id FROM refinement_workflows WHERE json_extract(plan,'$.stages[0].parent_id')=? AND NOT (state='CANCELLED' AND approval IS NULL) ORDER BY rowid DESC LIMIT 1")
      .get(parent) as { id: string } | null
    return row ? this.get(row.id) : null
  }
  recoverySource(id: string) {
    const previous = this.get(id)
    if (previous.state !== "APPROVED" || !this.refinements.store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()) return null
    const runs = previous.plan.stages.map((stage) => this.refinements.store.db.query("SELECT state,manifest FROM refinement_runs WHERE id=?")
      .get(stage.id) as { state: string; manifest: string | null } | null)
    if (runs[0]?.state !== "SUCCEEDED" || !runs[0].manifest || !runs[1] || !["FAILED", "CANCELLED"].includes(runs[1].state)) return null
    return { previous, reference: { id: previous.id, digest: previous.digest,
      af3_result_digest: digest(verifyRefinementResult(JSON.parse(runs[0].manifest), previous.plan.stages[0])) } }
  }
  verifyRetry(plan: RefinementWorkflow) {
    if (plan.recovery_of) {
      const prior = this.get(plan.recovery_of.id)
      if (prior.id === plan.id || prior.digest !== plan.recovery_of.digest || prior.state !== "APPROVED") throw new Error("Recovery source workflow changed")
      this.authorize(prior.plan.stages[1].id)
      if (digest(plan.stages[0]) !== digest(prior.plan.stages[0])) throw new Error("Recovery must reuse the exact original AF3 plan")
      const run = this.refinements.store.db.query("SELECT plan_digest,state,manifest,name,handle,submitted FROM refinement_runs WHERE id=?")
        .get(prior.plan.stages[0].id) as { plan_digest: string; state: string; manifest: string | null; name: string; handle: string | null; submitted: number | null } | null
      if (!run || run.state !== "SUCCEEDED" || !run.manifest || run.submitted === null ||
        run.plan_digest !== digest(prior.plan.stages[0]) || run.name !== `${prior.plan.stages[0].id}-af3` || run.handle !== run.name) throw new Error("Recovery requires verified original AF3 execution")
      const result = verifyRefinementResult(JSON.parse(run.manifest), prior.plan.stages[0])
      if (digest(result) !== plan.recovery_of.af3_result_digest) throw new Error("Reused AF3 result changed")
      const stopped = this.refinements.store.db.query("SELECT plan_digest,state FROM refinement_runs WHERE id=?")
        .get(prior.plan.stages[1].id) as { plan_digest: string; state: string } | null
      if (!stopped || stopped.plan_digest !== digest(prior.plan.stages[1]) || !["FAILED", "CANCELLED"].includes(stopped.state)) throw new Error("Recovery requires a terminal unsuccessful CovDock run")
      const next = plan.stages[1]
      if (next.environment_contract !== "no-loader-injection-v1") throw new Error("CovDock recovery requires isolated loader environment")
      const previous = prior.plan.stages[1]
      for (const key of ["parent_id", "parent_digest", "report_digest", "session", "directory", "input", "selection_id",
        "selection_manifest_digest", "engine", "quality_policy"] as const) {
        if (digest(next[key] ?? null) !== digest(previous[key] ?? null)) throw new Error("CovDock recovery changes scientific scope: " + key)
      }
      if (next.id === previous.id || next.created <= previous.created) throw new Error("Recovery needs a fresh CovDock execution identity")
      return
    }
    if (!plan.retry_of) return
    const previous = this.get(plan.retry_of.id)
    if (previous.id === plan.id || previous.digest !== plan.retry_of.digest || !this.failedBeforeCovdock(previous.id)) {
      throw new Error("Retry requires the exact terminal AF3 failure and unsubmitted CovDock")
    }
    for (const [index, stage] of plan.stages.entries()) {
      const prior = previous.plan.stages[index]
      for (const key of ["parent_id", "parent_digest", "report_digest", "session", "directory", "input", "selection_id",
        "selection_manifest_digest", "engine", "quality_policy", "resource", "minutes", "max_points"] as const) {
        if (digest(stage[key] ?? null) !== digest(prior[key] ?? null)) throw new Error("Repair retry changes approved scope: " + key)
      }
      if (stage.id === prior.id || stage.created <= prior.created) throw new Error("Retry must use new execution identities")
    }
  }
  create(value: RefinementWorkflow) {
    const plan = refinementWorkflow.parse(value)
    return this.refinements.store.db.transaction(() => {
      assertRefinementDraftOpen(this.refinements.store, plan.stages[0].parent_id, plan.id)
      this.verifyRetry(plan)
      const existing = this.refinements.store.db.query("SELECT id FROM refinement_workflows WHERE state!='CANCELLED' AND json_extract(plan,'$.stages[0].parent_id')=?")
        .all(plan.stages[0].parent_id) as { id: string }[]
      const ancestors = new Set<string>()
      for (let source = plan.recovery_of ?? plan.retry_of; source; ) {
        if (ancestors.has(source.id) || ancestors.size >= 100) throw new Error("Cyclic or excessive workflow recovery history")
        ancestors.add(source.id)
        const prior = this.get(source.id)
        if (prior.digest !== source.digest) throw new Error("Recovery history changed")
        source = prior.plan.recovery_of ?? prior.plan.retry_of
      }
      if (existing.some((row) => !ancestors.has(row.id))) {
        throw new Error("This core already has a pending or approved refinement workflow; recover it instead of creating another")
      }
      this.refinements.store.db.query("INSERT INTO refinement_workflows(id,plan,digest,state) VALUES(?,?,?,'DRAFT')")
        .run(plan.id, JSON.stringify(plan), digest(plan))
      plan.stages.forEach((stage, ordinal) => {
        if (plan.recovery_of && ordinal === 0) return
        const task = this.refinements.create(stage)
        this.refinements.store.db.query("INSERT INTO refinement_workflow_members(workflow_id,refinement_id,ordinal,digest) VALUES(?,?,?,?)")
          .run(plan.id, task.id, ordinal, task.digest)
      })
      this.refinements.store.event(plan.stages[0].parent_id, "refinement_workflow_created", { id: plan.id, digest: digest(plan) })
      return this.get(plan.id)
    }).immediate()
  }
  get(id: string): Workflow {
    const row = this.refinements.store.db.query("SELECT * FROM refinement_workflows WHERE id=?").get(id) as
      (Omit<Workflow, "plan"> & { plan: string }) | null
    if (!row) throw new Error("Unknown refinement workflow")
    const plan = refinementWorkflow.parse(JSON.parse(row.plan))
    if (row.id !== plan.id || row.digest !== digest(plan) || !["DRAFT", "APPROVED", "CANCELLED"].includes(row.state)) throw new Error("Immutable refinement workflow changed")
    const members = this.refinements.store.db.query("SELECT refinement_id,ordinal,digest FROM refinement_workflow_members WHERE workflow_id=? ORDER BY ordinal")
      .all(id) as { refinement_id: string; ordinal: number; digest: string }[]
    if (members.length !== (plan.recovery_of ? 1 : 2)) throw new Error("Workflow member set changed")
    plan.stages.forEach((stage, ordinal) => {
      if (plan.recovery_of && ordinal === 0) {
        if (this.refinements.get(stage.id).digest !== digest(stage)) throw new Error("Reused AF3 plan changed")
        return
      }
      const task = this.refinements.get(stage.id)
      const member = members.find((item) => item.ordinal === ordinal)!
      if (!member) throw new Error("Workflow member missing")
      if (member.refinement_id !== stage.id || member.ordinal !== ordinal || member.digest !== digest(stage) ||
        task.digest !== member.digest || task.approval !== null ||
        task.state !== (row.state === "APPROVED" ? "WORKFLOW_APPROVED" : row.state)) throw new Error("Workflow member plan or approval provenance changed")
    })
    return { ...row, plan }
  }
  approve(id: string, expected: string, context: Context) {
    // Reserve the writer before reading approval provenance. A deferred WAL
    // snapshot cannot upgrade after a daemon commits, even with busy_timeout.
    // This short synchronous transaction contains no remote IO or user prompt.
    return this.refinements.store.db.transaction(() => {
      const task = this.get(id)
      const plan = executableRefinementStages(task.plan)[0]
      if (task.state !== "DRAFT" || task.digest !== expected || plan.session !== context.sessionID || plan.directory !== context.directory) throw new Error("Workflow approval identity/state changed")
      const at = Date.now()
      if (at < plan.created || at > plan.approve_before) throw new Error("Workflow approval is outside its time window")
      context.abort.throwIfAborted()
      task.plan.stages.forEach((stage) => this.refinements.parent(stage))
      this.verifyRetry(task.plan)
      const grant = receipt.parse({ digest: expected, session: context.sessionID, at, boundary: "remote_compute",
        scope: task.plan.version, nonce: randomBytes(16).toString("hex") })
      this.refinements.store.db.query("UPDATE refinement_workflows SET state='APPROVED',approval=? WHERE id=?")
        .run(JSON.stringify(grant), id)
      executableRefinementStages(task.plan).forEach((stage) => this.refinements.store.db.query("UPDATE refinements SET state='WORKFLOW_APPROVED' WHERE id=?").run(stage.id))
      this.refinements.store.event(plan.parent_id, "refinement_workflow_approved", { id, ...grant })
      return this.get(id)
    }).immediate()
  }
  endApproval(id: string, expected: string, context: Pick<Context, "directory" | "sessionID">) {
    return this.refinements.store.db.transaction(() => {
      const task = this.get(id)
      const plan = executableRefinementStages(task.plan)[0]
      if (task.digest !== expected || plan.session !== context.sessionID || plan.directory !== context.directory) throw new Error("Workflow cleanup belongs to another plan/conversation/project")
      if (task.state !== "DRAFT" || task.approval) return task
      const executions = this.refinements.store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_runs'").get()
      if (executions && executableRefinementStages(task.plan).some((stage) => this.refinements.store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id))) throw new Error("Unapproved workflow has execution records; review required")
      this.refinements.store.db.query("UPDATE refinement_workflows SET state='CANCELLED' WHERE id=?").run(id)
      executableRefinementStages(task.plan).forEach((stage) => this.refinements.store.db.query("UPDATE refinements SET state='CANCELLED' WHERE id=?").run(stage.id))
      this.refinements.store.event(plan.parent_id, "refinement_workflow_approval_ended_without_grant", { id, digest: expected })
      return this.get(id)
    }).immediate()
  }
  authorize(id: string) {
    const member = this.refinements.workflow(id)
    if (!member) throw new Error("Missing workflow approval provenance")
    const task = this.get(member.workflow_id)
    const grant = receipt.parse(task.approval ? JSON.parse(task.approval) : null)
    const plan = executableRefinementStages(task.plan)[0]
    if (task.state !== "APPROVED" || grant.digest !== task.digest || grant.session !== plan.session ||
      grant.at < plan.created || grant.at > plan.approve_before) throw new Error("No valid exact-workflow approval")
    return task
  }
  ready(id: string) {
    const workflow = this.authorize(id)
    if (workflow.plan.recovery_of) this.verifyRetry(workflow.plan)
    if (workflow.plan.stages[0].id === id) return true
    const previous = workflow.plan.stages[0]
    const exists = this.refinements.store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_runs'").get()
    const run = exists ? this.refinements.store.db.query("SELECT plan_digest,state,manifest,handle,name,submitted FROM refinement_runs WHERE id=?").get(previous.id) as
      { plan_digest: string; state: string; manifest: string | null; handle: string | null; name: string; submitted: number | null } | null : null
    if (!run || run.state !== "SUCCEEDED") return false
    if (run.plan_digest !== digest(previous) || !run.manifest || run.submitted === null ||
      run.name !== `${previous.id}-af3` || run.handle !== run.name) throw new Error("Preceding AF3 execution identity changed")
    // SUCCEEDED alone is insufficient. Revalidate immutable candidate identity,
    // coverage and classified outcomes before releasing the second stage.
    verifyRefinementResult(JSON.parse(run.manifest), previous)
    return true
  }
}

// The runtime adapter must perform fresh checks for BOTH engines before and
// after the single native user confirmation. No production no-op verifier exists.
export async function proposeRefinementWorkflow(store: RefinementWorkflows, value: RefinementWorkflow, context: Context,
  verify: (plan: RefinementPlan) => Promise<void>) {
  const plan = refinementWorkflow.parse(value)
  const first = executableRefinementStages(plan)[0]
  if (first.session !== context.sessionID || first.directory !== context.directory) throw new Error("Workflow proposal belongs to another conversation/project")
  const check = async () => {
    store.verifyRetry(plan)
    for (const stage of executableRefinementStages(plan)) {
      context.abort.throwIfAborted()
      if (Date.now() < stage.created || Date.now() > stage.approve_before) throw new Error("Workflow proposal is outside its approval window")
      store.refinements.parent(stage)
      await verify(stage)
    }
    context.abort.throwIfAborted()
    if (Date.now() > first.approve_before) throw new Error("Workflow approval expired during verification")
  }
  await check()
  const task = store.create(plan)
  try {
    context.metadata({ title: plan.recovery_of ? "等待仅恢复 CovDock 的新确认（复用 AF3）" : "等待 AF3 + CovDock 精筛方案的一次确认", metadata: { task_id: task.id, digest: task.digest, state: "DRAFT" } })
    const parent = store.refinements.parent(first)
    store.refinements.store.event(first.parent_id, "refinement_workflow_approval_requested", { id: task.id, digest: task.digest })
    await context.ask({ permission: "remote_compute", patterns: [task.digest], always: [], metadata: { compute: {
      provider: "inspire", task_id: task.id, digest: task.digest, parent_task_id: first.parent_id,
      purpose: plan.recovery_of ? "复用已完成 AF3，仅恢复 CovDock（新授权、保留旧失败）" : (plan.retry_of ? "修复后重试（保留旧失败，独立新增预算） · " : "") + "AF3 结构评估 → CovDock 共价对接精筛（同一候选子集）", project: first.resource.project,
      account: parent.plan.connection.account, notebook: parent.plan.connection.notebook,
      candidates: first.input.candidates.length, selected_candidates: first.input.candidates,
      selection_id: first.selection_id, selection_manifest_digest: first.selection_manifest_digest,
      input_sha256: first.input.sha256, identity_sha256: first.input.identity_sha256,
      minutes: executableRefinementStages(plan).reduce((total, stage) => total + stage.minutes, 0), maximum_compute_points: plan.max_points,
      stages: executableRefinementStages(plan).map((stage) => ({ phase: stage.engine.name, task_id: stage.id, digest: digest(stage),
        minutes: stage.minutes, resource: stage.resource, maximum_compute_points: stage.max_points,
        engine_parameters: stage.engine, runtime_assets: stage.runtime, assets: stage.assets })),
      workflow_contract: plan.version, result_contract: first.result_contract,
      ...(plan.retry_of ? { retry_of: plan.retry_of } : {}),
      ...(plan.recovery_of ? { recovery_of: plan.recovery_of, reused_af3_task_id: plan.stages[0].id } : {}),
      ...(plan.delivery ? { delivery_policy: plan.delivery } : {}),
      runtime_admission: first.runtime_admission ?? null,
      scientific_quality_policy: first.quality_policy ?? null,
      engine_parameters: plan.stages.map((stage) => stage.engine), runtime_assets: plan.stages.map((stage) => stage.runtime),
      structure_site: "EGFR C797（AF3 序列位点及 CovDock 受体位点分别冻结）",
      code_release: first.code, approve_before: first.approve_before, finish_before: first.finish_before,
      execution: plan.execution, failure_policy: plan.failure_policy, quality_policy: plan.quality_policy,
      warning: (plan.recovery_of ? "本次只批准新的 CovDock 任务；AF3 保留原执行身份与结果，不重新计算、不重复计入新预算。" : "一次确认仅覆盖所列两阶段、固定候选和新增预算。") + (first.runtime_admission ? "设备运行检查在获批作业启动时进行，计入所列资源时限，失败即停止，不自动重试。" : "") + "AF3 结果核验完成后才提交 CovDock；后者独立对接同一分子名单，不自动使用 AF3 构象。保留质量淘汰结果，不扩充或替换名单；执行失败停止后续提交。排名分歧未校准，AF3 置信度不等于亲和力或活性。",
    } } })
    const current = store.get(task.id)
    if (current.state !== "DRAFT" || current.digest !== task.digest) throw new Error("Workflow draft changed during approval")
    await check()
    return store.approve(task.id, task.digest, context)
  } catch (error) {
    try { store.endApproval(task.id, task.digest, context) }
    catch { store.refinements.store.event(first.parent_id, "refinement_workflow_cleanup_requires_review", { id: task.id, digest: task.digest }) }
    throw error
  }
}
