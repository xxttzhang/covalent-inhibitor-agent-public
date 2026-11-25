import { randomUUID } from "node:crypto"
import type { ToolContext } from "@synsci/plugin/tool"
import { digest, type Task } from "./model"
import type { Store } from "./store"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows, refinementWorkflow, type RefinementWorkflow } from "./refinement-workflow"
import { RefinementRuns } from "./refinement-runs"
import { RefinementPreparation } from "./refinement-preparation"
import { RefinementReadbacks, type RefinementReadback } from "./refinement-readback"
import { evaluateRefinementQuality } from "./refinement-quality"
import { verifyRefinementResult } from "./refinement-result"
import { Inspire } from "./inspire"
import { readReport } from "./report"
import { ReviewRequired } from "./runner"

// No combined numeric score: AF3 confidence/geometry and docking scores answer
// different questions. Each original ID and selection reason survives triage.
export function combineRefinementReport(input: RefinementWorkflow, parent: Task,
  saved: [unknown, unknown], readbacks: [RefinementReadback, RefinementReadback]) {
  const workflow = refinementWorkflow.parse(input)
  const outcomes = workflow.stages.map((plan, index) => {
    if (plan.parent_id !== parent.id || plan.parent_digest !== parent.digest ||
      plan.session !== parent.plan.session || plan.directory !== parent.plan.directory) throw new ReviewRequired("Refinement report belongs to another core")
    const retained = verifyRefinementResult(saved[index], plan)
    const result = evaluateRefinementQuality(plan, parent, readbacks[index])
    if (digest(result) !== digest(retained)) throw new ReviewRequired("Fresh structural evaluation differs from the completed result")
    return result
  })
  const candidates = workflow.stages[0].input.candidates.map((source) => {
    const engines = outcomes.map((outcome, index) => {
      const row = outcome.candidates.find((candidate) => candidate.input_sdf_index === source.input_sdf_index)!
      if (!row || row.compound_id !== source.compound_id) throw new ReviewRequired("Combined report lost an original candidate")
      const measurements = readbacks[index].quality!.candidates.find((candidate) => candidate.input_sdf_index === source.input_sdf_index)!
      return { engine: outcome.engine, ...row, measurements: measurements.metrics,
        geometry: readbacks[index].geometry?.candidates.find((candidate) => candidate.input_sdf_index === source.input_sdf_index)?.measurements ?? null }
    })
    const status = engines.some((engine) => engine.status === "FAILED" ||
      engine.quality.assessment?.physical_validity === "UNKNOWN") ? "NOT_FULLY_EVALUATED" as const
      : engines.every((engine) => engine.quality.status === "PASSED") ? "BOTH_STRUCTURAL_QC_PASSED" as const
      : engines.some((engine) => engine.quality.status === "PASSED") ? "STRUCTURAL_QC_DISAGREEMENT" as const
      : "BOTH_STRUCTURAL_QC_REJECTED" as const
    return { ...source, status, af3: engines[0], covdock: engines[1] }
  })
  return { version: "screening-refinement-report-v1", workflow_id: workflow.id, workflow_digest: digest(workflow),
    ...(workflow.delivery?.final_ranking ? { final_ranking: workflow.delivery.final_ranking } : {}),
    parent_task_id: parent.id, parent_digest: parent.digest, core_report_digest: workflow.stages[0].report_digest,
    selection_id: workflow.stages[0].selection_id, selection_manifest_digest: workflow.stages[0].selection_manifest_digest,
    input_sha256: workflow.stages[0].input.sha256, input_identity_sha256: workflow.stages[0].input.identity_sha256,
    code: workflow.stages[0].code, quality_policy: workflow.stages[0].quality_policy,
    ...(workflow.recovery_of ? { recovery_of: workflow.recovery_of } : {}),
    candidate_count: candidates.length, candidates,
    counts: Object.fromEntries(["BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED"]
      .map((status) => [status, candidates.filter((candidate) => candidate.status === status).length])),
    stages: outcomes.map((outcome, index) => ({ engine: outcome.engine, task_id: outcome.task_id,
      code: workflow.stages[index].code, reused: !!workflow.recovery_of && index === 0,
      plan_digest: outcome.plan_digest, outcome_digest: digest(outcome), output_manifest_sha256: readbacks[index].manifest_sha256,
      success_count: outcome.success_count, failed_count: outcome.failed_count, quality_counts: outcome.quality_counts,
      artifacts: Object.entries(outcome.artifacts).sort(([a], [b]) => a.localeCompare(b))
        .map(([name, sha256]) => ({ name, sha256, remote_path: `${readbacks[index].output}/${name}` })) })),
    interpretation: {
      af3: "复合物结构、界面置信度与共价几何；不是亲和力或活性验证。",
      covdock: "固定受体与反应类型下的共价对接姿势、评分和内应变；不是实验活性证明。",
      uncertainty: "排名分歧是未校准的复核指标；共识和骨架多样性条目为对照。",
      combination: "不混加两引擎分数。双阶段质量通过仅指固定结构质检通过；分歧、拒绝和失败全部保留。",
      dependency: "CovDock 独立对接相同候选与登记受体，不使用 AF3 姿势。",
      limitation: "未验证完整拓扑、立体化学或亲和力；未确认实验命中。",
      ...(workflow.stages[0].quality_policy?.version === "refinement-structural-qc-v2-provisional" ? {
        evaluation_axes: "执行状态、物理有效性、证据完整性和复核准入分别报告；REJECTED 包含证据不足导致的未准入，不等于分子无效。",
        calibration: "provisional：保留既有数值，尚未完成科学校准；复核准入不代表 MD 就绪或计算授权。",
        strain: "仅使用 r_strain_Local_Strain_Energy；不以其他应变字段补填，也不将其解释为亲和力。",
      } : {}),
    }, hits_confirmed: false, affinity_validated: false }
}

// User-facing entry: read actual approved workflow history and freshly re-read
// engine files. Never creates a workflow, run, grant, workload or fallback result.
export async function readRefinementReport(store: Store, task: Task,
  context: Pick<ToolContext, "sessionID" | "directory" | "abort">, workflowID?: string) {
  if (context.directory !== task.plan.directory || context.sessionID !== task.plan.session) throw new Error("Refinement report belongs to another conversation/project")
  context.abort.throwIfAborted()
  const base = { task_id: task.id, core_state: task.state, report_ready: false, compute_restarted: false,
    platform_files_published: false, report: null, verification_started_at: Date.now() }
  // Planning events are deliberately not executions, even when named refineflow.
  if (!store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_workflows'").get()) {
    return { ...base, verification: "not_ready", reason: "NO_REFINEMENT_WORKFLOW", stages: [] }
  }
  const latest = (workflowID ? store.db.query("SELECT id FROM refinement_workflows WHERE id=? AND json_extract(plan,'$.stages[0].parent_id')=?").get(workflowID, task.id)
    : store.db.query("SELECT id FROM refinement_workflows WHERE json_extract(plan,'$.stages[0].parent_id')=? ORDER BY rowid DESC LIMIT 1").get(task.id)) as { id: string } | null
  if (!latest) return { ...base, verification: "not_ready", reason: "NO_REFINEMENT_WORKFLOW", stages: [] }
  const approvals = new Refinements(store)
  const workflows = new RefinementWorkflows(approvals)
  const workflow = workflows.get(latest.id)
  if (workflow.state !== "APPROVED") return { ...base, workflow_id: workflow.id,
    verification: "not_ready", reason: "WORKFLOW_NOT_APPROVED", stages: [] }
  workflows.authorize(workflow.plan.stages[1].id)
  if (workflow.plan.recovery_of) workflows.verifyRetry(workflow.plan)
  if (!store.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='refinement_runs'").get()) {
    return { ...base, workflow_id: workflow.id, verification: "not_ready", reason: "REFINEMENT_NOT_EXECUTED", stages: [] }
  }
  const runs = new RefinementRuns(approvals)
  const executions = workflow.plan.stages.map((stage) =>
    store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id) ? runs.get(stage.id) : null)
  const stages = executions.map((run, index) => ({ engine: workflow.plan.stages[index].engine.name,
    task_id: workflow.plan.stages[index].id, state: run?.state ?? "NOT_STARTED", job: run?.handle ?? null,
    submitted_at: run?.submitted ?? null, error: run?.error ?? null }))
  const status = { ...base, workflow_id: workflow.id, stages }
  if (executions.some((run) => !run || run.state !== "SUCCEEDED")) {
    return { ...status, verification: "not_ready", reason: "BOTH_REFINEMENT_STAGES_MUST_COMPLETE" }
  }
  const owner = randomUUID()
  try {
    for (const stage of workflow.plan.stages) {
      if (!runs.acquire(stage.id, owner)) throw new Error("Refinement result is currently being inspected")
    }
    const unchanged = () => {
      context.abort.throwIfAborted()
      if (digest(workflows.authorize(workflow.plan.stages[1].id)) !== digest(workflow)) throw new ReviewRequired("Workflow approval changed during report read")
      if (workflow.plan.recovery_of) workflows.verifyRetry(workflow.plan)
      workflow.plan.stages.forEach((stage, index) => {
        approvals.parent(stage)
        const run = runs.owned(stage.id, owner)
        const original = executions[index]!
        if (run.state !== "SUCCEEDED" || run.submitted === null || run.handle !== run.name ||
          run.manifest !== original.manifest || run.plan_digest !== original.plan_digest || run.command_digest !== original.command_digest) {
          throw new ReviewRequired("Refinement completion evidence changed during report read")
        }
      })
    }
    unchanged()
    const cli = new Inspire(task.plan.connection)
    const core = await readReport(task, store.stages(task.id), cli)
    if (!core.report_ready) throw new ReviewRequired("Core artifacts are not currently verified")
    const readbacks = new RefinementReadbacks(new RefinementPreparation(runs, cli))
    const files: RefinementReadback[] = []
    for (const stage of workflow.plan.stages) {
      unchanged()
      files.push(await readbacks.read(stage, runs.get(stage.id)))
      unchanged()
      // Keep all acquired leases live across bounded, sequential file reads.
      workflow.plan.stages.forEach((item) => runs.acquire(item.id, owner))
    }
    const report = combineRefinementReport(workflow.plan, task,
      [JSON.parse(executions[0]!.manifest!), JSON.parse(executions[1]!.manifest!)], [files[0], files[1]])
    unchanged()
    return { ...status, report_ready: true, verification: "verified", verified_at: Date.now(), report,
      delivery_note: "已重新核验两阶段源文件并汇总；尚未发布原生 Results。remote_path 是远端文件，不是浏览器下载链接。" }
  } catch (error) {
    return { ...status, verification: error instanceof ReviewRequired ? "evidence_rejected" : "verification_unavailable",
      reason: error instanceof Error ? error.message : "Refinement report read unavailable" }
  } finally {
    workflow.plan.stages.forEach((stage) => runs.release(stage.id, owner))
  }
}
