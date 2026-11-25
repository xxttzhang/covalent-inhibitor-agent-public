import { readStudy } from "./study-store"
import { readHypothesis } from "./study-hypothesis"
import { readReasoning } from "./study-reasoning"
import { readDeNovoPlan } from "./study-denovo-plan"
import { deNovoTasks, readDeNovoTask } from "../../src/tool/denovo-task"
import { readDeNovoResults } from "../../src/tool/denovo-results"
import { readDeNovoIndependentInput } from "./study-denovo-review"
import { deNovoBatchResume } from "./study-denovo-batch"

/** A projection of existing durable assets, never a second job/state ledger.
 * Reading a plan revalidates its study, hypothesis and content hashes.
 * PLAN_READY means eligible to request native approval, never approved.
 */
export async function deNovoWorkflow(directory: string, project: string, id: string) {
  const study = await readStudy(directory, project, id)
  if (study.denovoBranches) throw Error("Inspect parent branches, then select a child study")
  const route = study.routes["de-novo"]
  if (!route.selected || route.mode !== "new") throw Error("Select a new De novo route")
  const hypothesis = await readHypothesis(directory, study, "de-novo")
  const reasoning = await readReasoning(directory, study)
  const saved = await readDeNovoPlan(directory, study)
  const task = saved ? await readDeNovoTask(deNovoTasks, String(saved.plan.request.id),
    { directory, study: study.id, request: saved.plan.request }) : null
  const evaluation = task?.state === "COMPLETED" && saved
    ? await readDeNovoResults(deNovoTasks, task.id, { directory, study: study.id, request: saved.plan.request })
    : null
  const batch = evaluation?.state === "RESULTS_READ" && task ? await deNovoBatchResume(deNovoTasks, task.id) : null
  const stage = batch?.stage ?? (task ? "TASK_REVIEW" : saved ? "PLAN_READY" : !hypothesis ? "EVIDENCE_REQUIRED"
    : !reasoning ? "REASONING_REQUIRED" : "PLAN_REQUIRED")
  return {
    schema: "study-denovo-workflow-v1", route: "de-novo", workflow_id: study.id,
    stage, target: study.target, inputs: route.inputs, generation: route.generation,
    hypothesis_sha256: hypothesis?.sha256 ?? null,
    planning_session: reasoning?.session ?? null,
    plan_digest: saved?.planDigest ?? null,
    task_id: saved?.plan.request.id ?? null,
    input_hashes: saved?.plan.inputHashes ?? null,
    batch,
    compute_submitted_by_this_call: false,
    next: batch?.next ?? (task?.state === "COMPLETED" ? { tool: "denovo_generate", action: "select_batch", task_id: task.id,
        note: "Validate raw molecular identities, deduplicate and select diverse parents before prepare_batch. Generation geometry alone is not independent docking evidence." }
      : task ? { tool: "denovo_generate", action: "workflow", study_id: study.id,
        note: "Existing task recovered; no worker was launched. Inspect this task instead of recreating it." } : saved
      ? { tool: "denovo_generate", action: "create_study", study_id: study.id,
          native_approval_required: true, note: "Request approval for the frozen bound study; do not infer approval from this workflow or a system notification." }
      : { tool: hypothesis && reasoning ? "denovo_generate" : "discovery_study",
          action: !reasoning ? "start-reasoning" : !hypothesis ? "reasoning-status" : "plan_study",
          study_id: study.id }),
    execution: { state: task?.state ?? "not_submitted", task,
      authority: "native_task_ledger", observed_locally: true },
    assessment: { state: "not_evaluated" },
    independent_evaluation: await readDeNovoIndependentInput(directory, study),
    evaluation,
  }
}
