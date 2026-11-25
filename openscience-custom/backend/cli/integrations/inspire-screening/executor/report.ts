import { digest, verifyOutcome, type Outcome, type Stage, type Task } from "./model"
import { ReviewRequired, type Transport } from "./runner"

// Execution history and current artifact availability are distinct. This read
// never changes task state, submits/retries a job, or returns molecular tables.
export async function readReport(task: Task, stages: Stage[], transport: Pick<Transport, "verify">) {
  const started = Date.now()
  const base = { verification_started_at: started, task_id: task.id, plan_digest: task.digest }
  if (task.state !== "SUCCEEDED" || stages.length !== task.plan.steps.length ||
      stages.some((stage) => stage.state !== "SUCCEEDED" || !stage.manifest)) {
    return { ...base, report_ready: false, verification: "not_ready" as const,
      reason: "All core phases must have verified completion before report delivery", artifacts: [] }
  }
  const saved: Outcome[] = []
  try {
    if (digest(task.plan) !== task.digest || !task.approval || JSON.parse(task.approval).digest !== task.digest) {
      throw new Error("Report has no unchanged approved plan")
    }
    for (const [index, stage] of stages.entries()) {
      if (stage.task_id !== task.id || stage.ordinal !== index || stage.phase !== task.plan.steps[index].phase) {
        throw new Error("Report stage order or task identity changed")
      }
      saved.push(verifyOutcome(JSON.parse(stage.manifest!), task, stage, saved[index - 1] ?? null))
    }
  } catch (error) {
    return { ...base, report_ready: false, verification: "evidence_rejected" as const,
      reason: error instanceof Error ? error.message : "Invalid saved evidence", artifacts: [] }
  }
  for (const [index, stage] of stages.entries()) {
    const observed = await transport.verify(task, stage).then(
      (value) => ({ ok: true as const, value }),
      (error: unknown) => ({ ok: false as const, error }),
    )
    if (!observed.ok) {
      return { ...base, report_ready: false, verification: observed.error instanceof ReviewRequired
        ? "evidence_rejected" as const : "verification_unavailable" as const,
      failed_phase: stage.phase, artifacts: [],
      reason: observed.error instanceof Error ? observed.error.message : "Artifact verification unavailable",
      instruction: "Do not claim a usable report or restart computation; inspect evidence or retry this read only" }
    }
    try {
      const fresh = verifyOutcome(observed.value, task, stage, saved[index - 1] ?? null)
      if (digest(fresh) !== digest(saved[index])) throw new Error("Current evidence differs from the recorded verified manifest")
    } catch (error) {
      return { ...base, report_ready: false, verification: "evidence_rejected" as const,
        failed_phase: stage.phase, reason: error instanceof Error ? error.message : "Invalid current evidence", artifacts: [] }
    }
  }
  const root = `${task.plan.runtime.root}/tasks/${task.id}`
  return { ...base, report_ready: true, verification: "verified" as const, verified_at: Date.now(),
    artifacts: saved.map((result) => {
      const entries = Object.entries(result.artifacts).sort(([a], [b]) => a.localeCompare(b))
      return { phase: result.phase, manifest_digest: digest(result), manifest_path: `${root}/${result.phase}/manifest.json`,
        input_count: result.input_count, success_count: result.success_count, failed_count: result.failed_count,
        artifact_count: entries.length, truncated: entries.length > 50,
        files: entries.slice(0, 50).map(([name, sha256]) => ({ name, sha256, path: `${root}/${result.phase}/${name}` })) }
    }),
    delivery_note: "Checksums verified at read time; paths are remote Inspire files, not browser download links. Full inventory is in each manifest. No molecular table is included. Core screening only; AF3/CovDock refinement is not implied." }
}
