import z from "zod"
import { digest, resource, type Task } from "./model"
import type { Store } from "./store"
import { Inspire } from "./inspire"
import { settings } from "./host"
import { diagnoseRefinement, refinementDiagnosticSnapshot, refinementSettings } from "./refinement-diagnostic"
import { selection, selectRefinementProposal, readSelectionProposal, verifySelectionProposal } from "./selection-proposal"
import { defaultRefinementQuality, provisionalRefinementQuality, refinementQualityPolicy } from "./refinement-quality-policy"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"
import { planningEvent, retainPlanningEvent, verifyPlanningScope, refinementPlanningScope, type RefinementPlanningScope } from "./refinement-planning-scope"

// A versioned proposal default, not a calibrated scientific optimum or consent.
// Do not fill missing channel slots or inherit the core's uncapped budget.
const legacyVersion = "refinement-proposal-v3"
const legacyBoundVersion = "refinement-proposal-v4-bound-selection"
export const refinementProposalVersion = "refinement-proposal-v5"
export const boundRefinementProposalVersion = "refinement-proposal-v6-bound-selection"
const continuation = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("retry"), id: z.string().regex(/^refineflow-[a-f0-9]{24}$/), digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict(),
  z.object({ kind: z.literal("recovery"), id: z.string().regex(/^refineflow-[a-f0-9]{24}$/), digest: z.string().regex(/^[a-f0-9]{64}$/), af3_result_digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict(),
])
type Continuation = z.infer<typeof continuation>
type Version = typeof legacyVersion | typeof legacyBoundVersion | typeof refinementProposalVersion | typeof boundRefinementProposalVersion

// Recovery inherits only a verified frozen ancestor's policy. It is never a
// caller-selected downgrade, and it does not reuse that ancestor's approval.
function continuationPolicy(store: Store, task: Task, value: Continuation) {
  const workflows = new RefinementWorkflows(new Refinements(store))
  const previous = workflows.get(value.id)
  if (previous.digest !== value.digest || previous.plan.stages[0].parent_id !== task.id ||
    previous.plan.stages[0].parent_digest !== task.digest) throw Error("Proposal continuation source changed")
  workflows.authorize(previous.plan.stages[0].id)
  if (value.kind === "retry") {
    if (!workflows.failedBeforeCovdock(value.id)) throw Error("Proposal retry source is not terminal")
  } else {
    const source = workflows.recoverySource(value.id)
    if (!source || digest(source.reference) !== digest({ id: value.id, digest: value.digest,
      af3_result_digest: value.af3_result_digest })) throw Error("Proposal recovery evidence changed")
  }
  return refinementQualityPolicy.parse(previous.plan.stages[0].quality_policy)
}

function planningContinuation(store: Store, task: Task): Continuation | null {
  const workflows = new RefinementWorkflows(new Refinements(store))
  const previous = workflows.planningSource(task.id)
  if (!previous) return null
  if (workflows.failedBeforeCovdock(previous.id)) return { kind: "retry", id: previous.id, digest: previous.digest }
  const recovery = workflows.recoverySource(previous.id)
  return recovery ? { kind: "recovery", ...recovery.reference } : null
}
export const refinementSelectionPolicy = Object.freeze(selection.parse({ max_candidates: 8, max_fraction: 0.25,
  disagreement_threshold: 0.5, top_fraction: 0.2, uncertainty_count: 4, consensus_count: 2, diversity_count: 2 }))
const quotes = z.object({ af3: resource, covdock: resource, recorded_at: z.number().int().nonnegative() }).strict()
type Quotes = z.infer<typeof quotes>
type Subset = ReturnType<typeof verifySelectionProposal>

export function refinementProposal(store: Store, task: Task, subset: Subset, rates: Quotes, now = Date.now(), input?: RefinementPlanningScope) {
  return versionedProposal(store, task, subset, rates, now, input,
    input ? boundRefinementProposalVersion : refinementProposalVersion, input ? null : planningContinuation(store, task))
}

function versionedProposal(store: Store, task: Task, subset: Subset, rates: Quotes, now: number,
  input: RefinementPlanningScope | undefined, version: Version, resume: Continuation | null) {
  const scope = input ? verifyPlanningScope(store, task, input) : undefined
  const legacy = version === legacyVersion || version === legacyBoundVersion
  if (scope && resume) throw Error("Bound initial proposal cannot inherit a continuation")
  const policy = legacy ? defaultRefinementQuality : resume ? continuationPolicy(store, task, resume) : provisionalRefinementQuality
  const selected = verifySelectionProposal(subset, task, store.stages(task.id), scope?.selection.policy ?? refinementSelectionPolicy)
  if (scope && (selected.selection_id !== scope.selection.selection_id || selected.selection_manifest_digest !== scope.selection.selection_manifest_digest))
    throw Error("Refinement proposal cannot replace the workflow's reviewed subset")
  const live = quotes.parse(rates)
  if (live.af3.kind !== "job" || live.covdock.kind !== "hpc" ||
    [live.af3, live.covdock].some((spec) => task.plan.steps.some((step) => step.resource.project !== spec.project))) {
    throw new Error("Refinement resource roles/project differ from the completed core")
  }
  if (live.recorded_at > now || now - live.recorded_at > 5 * 60000) throw new Error("Resource quote is not recent")
  const environment = refinementDiagnosticSnapshot(store, task)
  const stages = [
    { engine: "af3", minutes: 120, resource: live.af3,
      question: "评估同一靶点与候选复合物的结构、界面置信度和共价几何；不是亲和力或活性验证。",
      parameters: { num_recycles: 3, num_seeds: 1, num_diffusion_samples: 5, run_data_pipeline: false,
        random_seed: 20260725, min_coverage: 0.95 },
      required_checks: ["原始分子 ID / 输入哈希对应", "置信度从原始 AF3 文件回读", "目标 Cys 共价连接、碰撞和几何质量"] },
    // Installed CovDock 2021-2 help gives ~1–3 h/compound in leadopt.
    // The registered runner uses NJOBS=1. Budget the upper documented value
    // plus setup allowance; this remains a fresh approval, not a runtime promise.
    { engine: "covdock", minutes: selected.selected_count * 180 + 60, resource: live.covdock,
      question: "在明确受体和反应类型下评估共价对接姿势及能量/应变；不是实验活性证明。",
      parameters: { mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.95 },
      required_checks: ["原始分子 ID 与同一候选子集对应", "受体 A:797 Cys / SG 对应", "实际共价姿势、键几何、碰撞与应变"] },
  ].map((stage) => ({ ...stage, proposed_points: stage.minutes / 60 * stage.resource.points_per_hour,
    candidate_count: selected.selected_count }))
  const blockers = [...new Set([
    ...(selected.selected_count === 0 ? ["NO_CANDIDATES_MEET_POLICY"] : []),
    ...(scope && stages.reduce((sum, stage) => sum + stage.proposed_points, 0) > scope.max_points + 1e-8 ? ["PROPOSED_REFINEMENT_BUDGET_EXCEEDED"] : []),
    ...(environment?.state === "AVAILABLE" ? environment.result.blockers.filter((code) => code !== "AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED") : ["ENVIRONMENT_DIAGNOSTIC_UNAVAILABLE"]),
  ])]
  return { version, ...(!legacy ? { continuation: resume } : {}),
    ...(scope ? { planning_scope: scope } : {}), task_id: task.id, plan_digest: task.digest, created_at: now,
    state: blockers.length ? "BLOCKED" as const : "PENDING_RUNTIME_CONFIRMATION" as const,
    selection: selected, environment, resource_quote_recorded_at: live.recorded_at,
    stages, proposed_points: stages.reduce((sum, stage) => sum + stage.proposed_points, 0),
    runtime_basis: `AF3 工程时限 120 分钟；CovDock 2021-2 本机帮助注明 leadopt 约 1–3 小时/分子，当前 NJOBS=1，按每个候选 180 分钟加准备余量 60 分钟提案，本次 ${selected.selected_count * 180 + 60} 分钟。不是实测耗时或完成保证；排队另计，必须重新审批，不继承旧授权。`,
    pricing_basis: "当前配额每实例时价 × 两阶段时限；不是账户账单上限或已批准预算。价格、镜像和可调度性在审批/提交前须重新核实。",
    selection_basis: scope ? `沿用工作流已评审的确切候选及显式策略：${scope.selection.reason}。未重新选择或填补名额；排名分歧不是已校准不确定性。`
      : "最多 8 个且不超过合格池 25%；4 个排名分歧复核 + 2 个共识对照 + 2 个骨架多样性对照。未用名额不补齐；这是待审阅默认策略，不是已校准不确定性。",
    execution: "AF3 后 CovDock 对同一分子子集分别评估；CovDock 使用已登记受体和核心候选，不默认采用 AF3 姿势。质量拒绝保留，执行失败停止。",
    quality_policy: { state: "PROPOSED_NOT_APPROVED", policy,
      policy_digest: digest(policy), min_engine_coverage: 0.95,
      note: legacy ? "拟定结构质检：目标共价连接明确，反应原子距离 1.5–2.2 Å，蛋白–配体非反应重原子近接触至少 1.2 Å；AF3 无已报告碰撞；CovDock 应变 ≤20 kcal/mol 且必要测量齐全。沿用项目几何/应变规则，未经活性标定。AF3 置信度描述性报告，不设亲和力阈值；未验证完整拓扑/立体化学。95% 仅为引擎覆盖率，全部候选被质检淘汰也必须如实保留。实际执行前须与候选、资源一并获得一次确认。" : resume && policy.version === "refinement-structural-qc-v1"
        ? "沿用已核验历史工作流的 v1 冻结策略；续跑需重新审批，不重新解释已有结果。旧版缺失处理与字段回退语义保持原样，尚未采用 v2 独立评价轴；历史 REJECTED 不能直接解释为分子无效。"
        : (resume ? "沿用已核验工作流的 v2 暂定策略；续跑需重新审批。" : "采用暂定 v2 结构质检；数值尚未统计校准。") +
          "计算状态、物理有效性与证据完整性分别报告；缺失不等于分子无效。CovDock v2 仅采用 r_strain_Local_Strain_Energy 原始局部应变字段，不能解释为相对母体应变。95% 为引擎覆盖率，质检与复核准入不证明活性或 MD 就绪；候选、资源和策略须经原生审批。" },
    blockers, ready_for_approval: false, resource_approval_granted: false, compute_submitted: false,
    startup_checks: "获批的 AF3 作业内先核验单 GPU、参数加载和设备运算；CovDock 作业内复核官方授权。检查时间计入阶段时限，失败停止且保留记录。CPU 诊断不是 GPU 推理完成。",
    report_ready: false, scientific_quality_verified: false,
    next: "先解决列出的运行条件并冻结科学质量门槛，再由平台展示一次两阶段原生审批；本提案不会创建可执行任务。" }
}

// No ask/create/submit/daemon API is reachable from this entry. The only remote
// writes are immutable subset evidence derived from the completed core.
export async function planRefinement(store: Store, task: Task, signal: AbortSignal, input?: RefinementPlanningScope) {
  const scope = input ? verifyPlanningScope(store, task, input) : undefined
  if (scope && planningEvent(store, task.id, "refinement_execution_proposal", scope))
    return refinementProposalSnapshot(store, task, scope)
  const identity = digest({ task, stages: store.stages(task.id) })
  const bound = () => {
    signal.throwIfAborted()
    if (identity !== digest({ task: store.get(task.id), stages: store.stages(task.id) })) throw new Error("Core changed during planning")
  }
  try {
    bound()
    const config = await settings()
    const transport = new Inspire(task.plan.connection)
    transport.bound(task)
    const cpu = resource.parse({ ...config.cpu, points_per_hour: 0 })
    const gpu = resource.parse({ ...config.gpu, points_per_hour: 0 })
    if ([cpu, gpu].some((spec) => task.plan.steps.some((step) => step.resource.project !== spec.project))) throw new Error("Resource project changed")
    await diagnoseRefinement(store, task.id, signal)
    bound()
    const profile = await refinementSettings()
    const environment = refinementDiagnosticSnapshot(store, task)
    if (environment?.state === "AVAILABLE" && environment.result.profile_digest !== digest(profile)) throw new Error("Runtime profile changed")
    const subset = scope ? await readSelectionProposal(store, task, scope.selection, transport, signal)
      : await selectRefinementProposal(store, task, refinementSelectionPolicy, transport, signal)
    bound()
    const [af3, covdock] = await Promise.all([transport.live(gpu), transport.live(cpu)])
    bound()
    if (digest(await settings()) !== digest(config) || digest(await refinementSettings()) !== digest(profile)) throw new Error("Planning configuration changed")
    const rates = { af3: { ...gpu, points_per_hour: af3 }, covdock: { ...cpu, points_per_hour: covdock }, recorded_at: Date.now() }
    const value = refinementProposal(store, store.get(task.id), subset, rates, Date.now(), scope)
    retainPlanningEvent(store, task.id, "refinement_execution_proposal", { state: "AVAILABLE", version: value.version, value, digest: digest(value) }, scope)
  } catch {
    retainPlanningEvent(store, task.id, "refinement_execution_proposal", { state: "UNAVAILABLE", version: scope ? boundRefinementProposalVersion : refinementProposalVersion,
      reason: signal.aborted ? "PLANNING_ABORTED" : "PLANNING_EVIDENCE_OR_RESOURCE_READ_FAILED" }, scope)
  }
  return refinementProposalSnapshot(store, store.get(task.id), scope)
}

export function refinementProposalSnapshot(store: Store, task: Task, scope?: RefinementPlanningScope) {
  const row = planningEvent(store, task.id, "refinement_execution_proposal", scope)
  if (!row) return null
  const base = { task_id: task.id, recorded_at: row.at, ready_for_approval: false, compute_submitted: false,
    note: "保存时的方案快照，不是当前资源/文件可用性或执行审批。" }
  try {
    const saved = JSON.parse(row.payload)
    const bound = saved.planning_scope ? refinementPlanningScope.parse(saved.planning_scope) : undefined
    const versions = bound ? [legacyBoundVersion, boundRefinementProposalVersion] : [legacyVersion, refinementProposalVersion]
    if (saved.state !== "AVAILABLE" || !versions.includes(saved.version) || saved.value?.version !== saved.version
      || digest(saved.value) !== saved.digest || digest(saved.value.planning_scope ?? null) !== digest(bound ?? null)) throw new Error("Invalid proposal")
    const value = saved.value as ReturnType<typeof refinementProposal>
    const legacy = saved.version === legacyVersion || saved.version === legacyBoundVersion
    const resume = legacy || saved.value.continuation === null ? null : continuation.parse(saved.value.continuation)
    const expected = versionedProposal(store, task, value.selection, { af3: value.stages[0].resource,
      covdock: value.stages[1].resource, recorded_at: value.resource_quote_recorded_at }, value.created_at, bound, saved.version, resume)
    if (digest(expected) !== saved.digest) throw new Error("Proposal or latest environment changed")
    return { ...base, state: "AVAILABLE" as const, proposal: expected }
  } catch {
    return { ...base, state: "UNAVAILABLE" as const, proposal: null,
      note: "最新方案不可用、格式无效或依赖证据已变化；未回退旧方案，也未启动计算。" }
  }
}
