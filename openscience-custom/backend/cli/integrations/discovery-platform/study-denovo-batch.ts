import path from "node:path"
import { lstat, realpath } from "node:fs/promises"
import z from "zod"

/** Local artifacts indicate where to resume, never whether a remote job ran. */
export async function deNovoBatchResume(base: string, id: string) {
  if (!/^denovo-[a-f0-9]{24}$/.test(id)) throw Error("Invalid native task ID")
  const root = path.resolve(base, id)
  const phases = [
    ["batch-report.json", "denovo-candidate-ranking-v1", "BATCH_REPORT_REVIEW", "report_batch"],
    ["batch-glide-plan.json", "denovo-batch-glide-plan-v1", "BATCH_GLIDE_OBSERVATION", "batch_prereaction_status"],
    ["batch-prereaction-input.json", "denovo-batch-prereaction-input-v1", "BATCH_PREREACTION_INPUT_FROZEN", "plan_batch_prereaction"],
    ["batch-lo-retry-plan.json", "denovo-batch-lo-plan-v1", "BATCH_LO_OBSERVATION", "batch_lo_status"],
    ["batch-lo-retry-input.json", "denovo-batch-lo-input-v1", "BATCH_LO_INPUT_FROZEN", "plan_batch_lo"],
    ["batch-lo-plan.json", "denovo-batch-lo-plan-v1", "BATCH_LO_OBSERVATION", "batch_lo_status"],
    ["batch-lo-input.json", "denovo-batch-lo-input-v1", "BATCH_LO_INPUT_FROZEN", "plan_batch_lo"],
    ["batch-vs-plan.json", "denovo-batch-vs-plan-v1", "BATCH_VS_OBSERVATION", "batch_vs_status"],
    ["batch-input/registration.json", null, "BATCH_REGISTERED", "plan_batch_vs"],
    ["batch-input/input.json", null, "BATCH_INPUT_FROZEN", "register_batch"],
    ["batch-selection.json", null, "BATCH_SELECTED", "prepare_batch"],
  ] as const
  for (const [file, schema, stage, action] of phases) {
    const filename = path.join(root, file)
    if (!await Bun.file(filename).exists()) continue
    if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
      throw Error("Batch recovery requires a regular file without symlinks")
    const value = z.record(z.string(), z.unknown()).parse(await Bun.file(filename).json())
    if (schema && value.schema !== schema) throw Error("Batch recovery schema differs")
    if (value.task_id !== undefined && value.task_id !== id) throw Error("Batch recovery belongs to another task")
    return { stage, local_artifact: file, remote_execution_verified: false,
      next: { tool: "denovo_generate", action, task_id: id,
        note: "Resume by revalidating this artifact and observing the original remote request. A plan is not a submission receipt. Submit only after authoritative NO_RECEIPT; never repeat a RESERVED, UNCERTAIN, QUEUED or RUNNING request. After completion: read_batch_vs_products → prepare_batch_lo, evaluate_batch_lo → prepare_batch_prereaction, evaluate_batch_prereaction → report_batch." } }
  }
  return { stage: "BATCH_SELECTION_REQUIRED", remote_execution_verified: false,
    next: { tool: "denovo_generate", action: "select_batch", task_id: id,
      note: "Validate raw chemical identities and select diverse parents before independent preparation." } }
}
