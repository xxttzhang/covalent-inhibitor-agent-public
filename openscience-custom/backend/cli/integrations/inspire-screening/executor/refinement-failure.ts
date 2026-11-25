import z from "zod"
import { randomUUID, createHash } from "node:crypto"
import type { ToolContext } from "@synsci/plugin/tool"
import { canonical, digest, type Task } from "./model"
import type { Store } from "./store"
import type { RefinementPlan } from "./refinement-approval"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"
import { RefinementRuns } from "./refinement-runs"
import { Inspire, observation, projectObservation, quote } from "./inspire"
import { ReviewRequired } from "./runner"
import reader from "./refinement_failure.py" with { type: "text" }

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const relative = z.string().min(1).max(2048).refine((value) => !/[\\\x00-\x1f\x7f]/.test(value) &&
  value.split("/").every((part) => part && part !== "." && part !== ".."))
const receipt = z.object({ version: z.literal("refinement-failure-files-v1"), id: z.string(), digest: hash,
  parent_id: z.string(), engine: z.enum(["af3", "covdock"]), directory: z.string(), plan_sha256: hash,
  files: z.array(z.object({ name: relative, bytes: z.number().int().nonnegative().max(128 * 1024 * 1024), sha256: hash }).strict()).min(1).max(2000),
  missing: z.array(z.string()).max(3), excluded_caches: z.array(z.string()).max(1),
  scientific_quality_verified: z.literal(false), compute_restarted: z.literal(false) }).strict()

export function failurePacket(plan: RefinementPlan, parent: Task) {
  if (plan.parent_id !== parent.id || plan.parent_digest !== parent.digest ||
    plan.session !== parent.plan.session || plan.directory !== parent.plan.directory) throw new ReviewRequired("Failure read belongs to another core")
  return { root: parent.plan.runtime.root, parent: parent.id, task: plan.id, engine: plan.engine.name, digest: digest(plan),
    plan_sha256: createHash("sha256").update(JSON.stringify(canonical({ plan, digest: digest(plan) })) + "\n").digest("hex") }
}

export function verifyRefinementFailure(value: unknown, plan: RefinementPlan, parent: Task) {
  const result = receipt.parse(value)
  const packet = failurePacket(plan, parent)
  const directory = `${packet.root}/tasks/${parent.id}/refinements/${plan.id}`
  const controls = ["plan.json", "runtime-admission.json", "execution.log"]
  if (result.id !== plan.id || result.digest !== packet.digest || result.parent_id !== parent.id ||
    result.engine !== plan.engine.name || result.directory !== directory || result.plan_sha256 !== packet.plan_sha256 ||
    new Set(result.files.map((file) => file.name)).size !== result.files.length ||
    result.files.some((file) => !controls.includes(file.name) && !file.name.startsWith(plan.engine.name + "/")) ||
    result.files.some((file) => file.name.startsWith(plan.engine.name + "/jax_cache/")) ||
    result.files.reduce((sum, file) => sum + file.bytes, 0) > 1024 * 1024 * 1024 ||
    result.files.find((file) => file.name === "plan.json")?.sha256 !== packet.plan_sha256 ||
    result.excluded_caches.some((name) => name !== `${plan.engine.name}/jax_cache`) ||
    new Set(result.missing).size !== result.missing.length || result.missing.some((name) =>
      !["runtime-admission.json", "execution.log", `${plan.engine.name}/`].includes(name) ||
      result.files.some((file) => name.endsWith("/") ? file.name.startsWith(name) : file.name === name))) {
    throw new ReviewRequired("Failed-run inventory differs from the exact approved scope")
  }
  return result
}

export async function readRefinementFailure(store: Store, task: Task,
  context: Pick<ToolContext, "sessionID" | "directory" | "abort">, workflowID?: string,
  cli: Pick<Inspire, "bound" | "call"> = new Inspire(task.plan.connection)) {
  if (context.sessionID !== task.plan.session || context.directory !== task.plan.directory) throw new ReviewRequired("Failure evidence belongs to another conversation")
  context.abort.throwIfAborted()
  const base = { task_id: task.id, report_ready: false as const, failure_evidence_ready: false,
    compute_restarted: false as const, scientific_quality_verified: false as const, platform_files_published: false as const }
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()) return { ...base, reason: "NO_REFINEMENT_WORKFLOW" }
  const selected = (workflowID ? store.db.query("SELECT id FROM refinement_workflows WHERE id=? AND json_extract(plan,'$.stages[0].parent_id')=?").get(workflowID, task.id)
    : store.db.query("SELECT id FROM refinement_workflows WHERE json_extract(plan,'$.stages[0].parent_id')=? ORDER BY rowid DESC LIMIT 1").get(task.id)) as { id: string } | null
  if (!selected) return { ...base, reason: "NO_REFINEMENT_WORKFLOW" }
  const approvals = new Refinements(store)
  const workflows = new RefinementWorkflows(approvals)
  const workflow = workflows.get(selected.id)
  if (workflow.state !== "APPROVED") return { ...base, reason: "WORKFLOW_NOT_APPROVED" }
  workflows.authorize(workflow.plan.stages[1].id)
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()) return { ...base, reason: "REFINEMENT_NOT_EXECUTED" }
  const runs = new RefinementRuns(approvals)
  const executions = workflow.plan.stages.map((stage) => store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(stage.id) ? runs.get(stage.id) : null)
  if (executions.some((run) => !run || !["FAILED", "CANCELLED", "SUCCEEDED", "NEEDS_REVIEW"].includes(run.state)) ||
    !executions.some((run) => run && ["FAILED", "CANCELLED", "NEEDS_REVIEW"].includes(run.state))) return { ...base, reason: "NO_TERMINAL_FAILED_WORKFLOW" }
  const stages = executions.map((run, index) => ({ task_id: workflow.plan.stages[index].id,
    engine: workflow.plan.stages[index].engine.name, state: run!.state, job: run!.handle,
    submitted_at: run!.submitted, error: run!.error }))
  const owner = randomUUID()
  const stable = (run: NonNullable<typeof executions[number]>) => {
    const { lease_owner, lease_until, ...value } = run
    return value
  }
  const files: z.infer<typeof receipt>[] = []
  const termination: { task_id: string; remote_state: string; check: "before_files" | "after_files" }[] = []
  const unavailable: { task_id: string; reason: string }[] = []
  store.db.exec("CREATE TABLE IF NOT EXISTS refinement_failure_witness(id TEXT PRIMARY KEY,plan_digest TEXT NOT NULL,receipt TEXT NOT NULL)")
  const heartbeat = setInterval(() => workflow.plan.stages.forEach((stage) => runs.renew(stage.id, owner)), 10000)
  try {
    for (const stage of workflow.plan.stages) if (!runs.acquire(stage.id, owner)) throw new ReviewRequired("Failure evidence is already being inspected")
    const bound = () => {
      context.abort.throwIfAborted()
      cli.bound(task)
      if (digest(workflows.authorize(workflow.plan.stages[1].id)) !== digest(workflow)) throw new ReviewRequired("Failure workflow approval changed")
      workflow.plan.stages.forEach((stage, index) => {
        approvals.parent(stage)
        if (digest(stable(runs.owned(stage.id, owner))) !== digest(stable(executions[index]!))) throw new ReviewRequired("Failed execution state changed during inspection")
      })
    }
    bound()
    for (const [index, stage] of workflow.plan.stages.entries()) {
      if (executions[index]!.submitted === null) {
        unavailable.push({ task_id: stage.id, reason: "NOT_SUBMITTED_NO_REMOTE_EXECUTION_CLAIM" })
        continue
      }
      if (executions[index]!.handle !== executions[index]!.name) throw new ReviewRequired("Failed workload has no reconciled terminal identity")
      const packet = failurePacket(stage, task)
      const command = ["env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
        "python3", "-I", "-B", "-c", reader, Buffer.from(JSON.stringify(packet)).toString("base64")].map(quote).join(" ")
      const read = async () => {
        bound()
        const value = z.object({ returncode: z.number(), output: z.string().max(1024 * 1024) }).parse(await cli.call([
          "notebook", "exec", task.plan.connection.notebook, "--workspace", task.plan.connection.workspace, "--timeout", "90", command]))
        bound()
        if (value.returncode !== 0) throw new ReviewRequired("Failure inventory unavailable; no successful output may be inferred")
        return verifyRefinementFailure(JSON.parse(value.output), stage, task)
      }
      // NEEDS_REVIEW can also mean an unresolved or still-running submission.
      // Require live, exact resource identity and termination around the reads;
      // scheduler success only permits evidence inspection, never scientific acceptance.
      const terminal = async (check: "before_files" | "after_files") => {
        if (executions[index]!.state !== "NEEDS_REVIEW") return
        bound()
        const raw = await cli.call([stage.resource.kind, "status", executions[index]!.name, "--workspace", stage.resource.workspace])
        const detail = z.object({ project: z.string().min(1), compute_group: z.string().min(1),
          resource: z.object({ gpu: z.number(), cpu: z.number(), memory_gib: z.number(), nodes: z.number() }),
        }).passthrough().parse(raw)
        const normalized = detail.project !== stage.resource.project
          ? projectObservation(raw, stage.resource.project, await cli.call(["project", "detail", stage.resource.project])) : raw
        bound()
        const status = observation(normalized, executions[index]!, stage.resource)
        if (!["succeeded", "failed", "cancelled"].includes(status.state)) {
          throw new ReviewRequired("Review-stage workload is not confirmed terminal; failure inventory remains unavailable")
        }
        termination.push({ task_id: stage.id, remote_state: status.state, check })
      }
      await terminal("before_files")
      const first = await read()
      store.db.query("INSERT OR IGNORE INTO refinement_failure_witness(id,plan_digest,receipt) VALUES(?,?,?)")
        .run(stage.id, digest(stage), JSON.stringify(first))
      const saved = store.db.query("SELECT plan_digest,receipt FROM refinement_failure_witness WHERE id=?").get(stage.id) as { plan_digest: string; receipt: string }
      if (saved.plan_digest !== digest(stage) || digest(verifyRefinementFailure(JSON.parse(saved.receipt), stage, task)) !== digest(first)) throw new ReviewRequired("Original failure witness changed; preserve the first inventory")
      const second = await read()
      if (digest(second) !== digest(first)) throw new ReviewRequired("Failure files changed between reads")
      await terminal("after_files")
      files.push(second)
    }
    bound()
    return { ...base, workflow_id: workflow.id, failure_evidence_ready: true, verification: "verified_file_identity_only",
      stages, files, unavailable, termination, candidates: workflow.plan.stages[0].input.candidates,
      note: "仅核验失败现场文件身份；未提交阶段明确保留。缺失日志不推测原因，原始输出未经科学验收，尚未自动回传原生 Results。" }
  } catch (error) {
    return { ...base, workflow_id: workflow.id, stages, verification: "unavailable",
      reason: error instanceof ReviewRequired ? error.message : "FAILURE_EVIDENCE_READ_UNAVAILABLE" }
  } finally {
    clearInterval(heartbeat)
    workflow.plan.stages.forEach((stage) => runs.release(stage.id, owner))
  }
}

// Complete inventories remain in the immutable first witnesses and audit event.
// Do not overflow the chat tool limit with hundreds of AF3 intermediate files.
export function refinementFailureSummary(value: Awaited<ReturnType<typeof readRefinementFailure>>) {
  if (!("files" in value) || !value.files) return value
  const { files, ...summary } = value
  return { ...summary, inventories: files.map((file) => ({ task_id: file.id, engine: file.engine,
    file_count: file.files.length, total_bytes: file.files.reduce((total, entry) => total + entry.bytes, 0),
    inventory_sha256: digest(file), missing: file.missing, excluded_caches: file.excluded_caches })),
    inventory_storage: "Complete file identities retained in platform refinement_failure_witness and audit event; not browser downloads",
    interpretation: "NEEDS_REVIEW is the scientific review state, not evidence that the remote job is running. Successful termination checks only establish that files can be inspected; no structures, scores or integrated report are accepted by this failure read." }
}
