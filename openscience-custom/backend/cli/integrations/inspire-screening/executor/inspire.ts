import { requireDockingEngine } from "../../../src/session/docking-policy"
import z from "zod"
import { deflateSync } from "node:zlib"
import { connection, digest, resource, type Plan, type Stage, type Task } from "./model"
import { ReviewRequired, type Observation, type Transport } from "./runner"

export { connection } from "./model"
export type Connection = z.infer<typeof connection>
export class CLIError extends Error {
  constructor(readonly code: string) { super(`InspireSkill: ${code}; no automatic resubmission`) }
}
export function cliResponse(stdout: string, stderr: string, exit: number) {
  const parse = (text: string) => {
    try { return z.object({ success: z.boolean(), data: z.unknown().optional(),
      error: z.object({ code: z.unknown().optional(), type: z.string().optional(), message: z.string().optional() }).optional() }).parse(JSON.parse(text)) }
    catch { return undefined }
  }
  const output = parse(stdout)
  // InspireSkill emits failures on stderr, sometimes with empty stdout. Never
  // mistake that explicit rejection for a transient JSON/observation failure.
  if (exit !== 0 || output?.success === false) {
    const failure = [parse(stderr), output].find((value) => value?.success === false)
    // A known installation/platform contract failure, not an ambiguous create
    // response. Use a fixed public code; never forward arbitrary CLI messages.
    if (failure?.error?.message === "Could not resolve the selected workspace's scheduling policy.")
      throw new CLIError("workspace_scheduling_policy_unavailable")
    const code = [failure?.error?.code, failure?.error?.type].find((value) =>
      typeof value === "string" && /^[A-Za-z][A-Za-z0-9_]{0,80}$/.test(value))
    throw new CLIError(typeof code === "string" ? code : "request_failed")
  }
  if (!output || !output.success) throw new CLIError("invalid_response")
  return output.data
}
export const quote = (value: string) => "'" + value.replaceAll("'", "'\"'\"'") + "'"

export function stagingPayload(task: Pick<Task, "plan" | "digest">) {
  const raw = Buffer.from(JSON.stringify({ plan: task.plan, digest: task.digest }))
  if (raw.length > 4 * 1024 * 1024) throw new ReviewRequired("Task plan exceeds the bounded staging size; no remote action sent")
  const encoded = deflateSync(raw, { level: 9 }).toString("base64")
  if (encoded.length > 64 * 1024) throw new ReviewRequired("Task plan exceeds the bounded command transport; no remote action sent")
  return encoded
}

export function createArgs(plan: Plan, stage: Stage, dry: boolean) {
  const step = plan.steps[stage.ordinal]
  if (stage.phase !== step.phase || stage.name !== `${plan.id}-${stage.phase}`) throw new Error("Unexpected stage identity")
  const spec = step.resource
  const [, cpus, memory] = spec.quota.split(",").map(Number)
  const command = `cd ${quote(plan.runtime.project)} && source ${quote(plan.runtime.activation)} >/dev/null && export OMP_NUM_THREADS=${cpus} OPENBLAS_NUM_THREADS=${cpus} MKL_NUM_THREADS=${cpus} && exec python ${quote(plan.runtime.worker)} run ${quote(plan.id)} ${quote(stage.phase)} ${quote(digest(plan))}`
  const common = [spec.kind, "create", "--name", stage.name, "--workspace", spec.workspace, "--project", spec.project,
    "--group", spec.group, "--quota", spec.quota, "--image", spec.image,
    "--priority", String(spec.priority), "--max-time", String(step.minutes / 60), "--no-enable-notification", "--public-path-readonly"]
  if (spec.kind === "hpc") return [...common, "--image-type", spec.image_type, "--instance-count", "1", "--number-of-tasks", "1", "--cpus-per-task", String(cpus),
    "--memory-per-cpu", String(Math.max(1, Math.floor(memory / cpus) - 1)), "--disable-hyper-threading",
    "--entrypoint", `srun bash -lc ${quote(command)}`, ...(dry ? ["--dry-run"] : [])]
  return [...common, "--nodes", "1", "--shm-size", String(Math.min(8, memory)), "--no-auto-fault-tolerance",
    "--command", command, ...(dry ? ["--dry-run"] : [])]
}

// Status uses an English namespace while creation accepts the project display
// name. Accept the alias only when Live project detail proves that exact pair.
export function projectObservation(input: unknown, approved: string, identity: unknown) {
  const detail = z.object({ project: z.string() }).passthrough().parse(input)
  const project = z.object({ name: z.string().min(1), english_name: z.string().min(1) }).parse(identity)
  if (![project.name, project.english_name].includes(approved) || ![project.name, project.english_name].includes(detail.project)) {
    throw new ReviewRequired("Observed project is not the approved project's verified name/namespace")
  }
  return { ...detail, project: approved }
}

export function observation(input: unknown, expected: Pick<Stage, "name">, spec: z.infer<typeof resource>): Observation {
  const detail = z.object({ name: z.string(), status: z.string(), project: z.string().optional(),
    compute_group: z.string().optional(), workspace: z.string().optional(), steps: z.string().optional(),
    resource: z.object({ gpu: z.number(), cpu: z.number(), memory_gib: z.number(), nodes: z.number() }).optional(),
  }).passthrough().parse(input)
  if (detail.name !== expected.name || (detail.project && detail.project !== spec.project) ||
      (detail.compute_group && detail.compute_group !== spec.group) || (detail.workspace && detail.workspace !== spec.workspace)) throw new ReviewRequired("Observed job identity/resource differs from approval")
  if (detail.resource && (detail.resource.nodes !== 1 || `${detail.resource.gpu},${detail.resource.cpu},${detail.resource.memory_gib}` !== spec.quota)) {
    throw new ReviewRequired("Observed node count/quota differs from approval")
  }
  const state = detail.status.toUpperCase()
  if (["SUCCEEDED", "SUCCESS", "SUCCEEDED_RETAINING", "JOB_SUCCEEDED"].includes(state)) {
    if (spec.kind === "hpc" && detail.steps !== "1/1") throw new Error("HPC finished without the expected completed Slurm step")
    return { state: "succeeded", handle: expected.name }
  }
  if (["FAILED", "FAILED_RETAINING", "ERROR", "JOB_FAILED", "TIMED_OUT"].includes(state)) return { state: "failed", handle: expected.name }
  if (["STOPPED", "CANCELLED", "CANCELED", "JOB_STOPPED"].includes(state)) return { state: "cancelled", handle: expected.name }
  if (["RUNNING", "JOB_RUNNING"].includes(state)) return { state: "running", handle: expected.name }
  if (["PENDING", "QUEUING", "QUEUEING", "QUEUED", "CREATING", "SUBMITTED", "INITIALIZING", "JOB_PENDING", "JOB_QUEUING", "JOB_CREATING"].includes(state)) return { state: "pending", handle: expected.name }
  throw new Error("Unrecognized remote status; preserve handle and inspect, do not restart: " + state)
}

export class Inspire implements Transport {
  constructor(readonly config: Connection) { connection.parse(config) }
  bound(task: Task) {
    if (digest(this.config) !== digest(task.plan.connection)) throw new ReviewRequired("Connection differs from the approved account/Notebook; no remote action sent")
  }
  async call(args: string[], timeout = 110000) {
    const child = Bun.spawn([this.config.cli, "--json", "--no-env-file", "--account", this.config.account, ...args],
      { stdin: "ignore", stdout: "pipe", stderr: "pipe" })
    const timer = setTimeout(() => child.kill("SIGKILL"), timeout)
    try {
      const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
      return cliResponse(out, err, code)
    } finally { clearTimeout(timer) }
  }
  async remote(runtime: Plan["runtime"], args: string[]) {
    const command = `cd ${quote(runtime.project)} && source ${quote(runtime.activation)} >/dev/null && python ${quote(runtime.worker)} ${args.map(quote).join(" ")}`
    // Linux limits each exec argument (not just argv in total). Leave room for
    // the Notebook transport's shell wrapper; never send an oversized command.
    if (Buffer.byteLength(command) > 96 * 1024) throw new ReviewRequired("Remote command exceeds the bounded transport; no remote action sent")
    const value = z.object({ returncode: z.number(), output: z.string() }).parse(await this.call(["notebook", "exec", this.config.notebook,
      "--workspace", this.config.workspace, "--timeout", "90", command]))
    if (value.returncode !== 0) throw new CLIError("remote_worker_failed")
    return JSON.parse(value.output) as unknown
  }
  async prepare(task: Task) {
    this.bound(task)
    const payload = stagingPayload(task)
    const result = z.object({ staged: z.literal(true), id: z.literal(task.id), digest: z.literal(task.digest) })
      .parse(await this.remote(task.plan.runtime, ["freeze-compressed", payload]))
    if (!result.staged) throw new Error("Task staging failed")
  }
  async live(spec: z.infer<typeof resource>) {
    const result = z.object({ items: z.array(z.object({ compute_group: z.string(), quota: z.string(),
      points_per_hour: z.number().nonnegative().nullable(), priority: z.string() }).passthrough()) }).parse(
      await this.call([spec.kind, "quota", "--workspace", spec.workspace, "--group", spec.group, "--all"]))
    const row = result.items.find((item) => item.compute_group === spec.group && item.quota === spec.quota)
    if (!row || row.points_per_hour === null) throw new Error("Live quota/rate unavailable")
    if (row.priority === "low" && spec.priority !== 1) throw new Error("Quota only permits preemptible priority")
    return row.points_per_hour
  }
  async preflight(task: Task, stage: Stage) {
    this.bound(task)
    const price = await this.live(task.plan.steps[stage.ordinal].resource)
    if (price !== task.plan.steps[stage.ordinal].resource.points_per_hour) throw new ReviewRequired("Price changed; task needs a new scoped approval")
    const assets = z.object({ assets: z.record(z.string(), z.string()) }).passthrough().parse(await this.remote(task.plan.runtime,
      ["snapshot", ...(task.plan.research_inputs ? [Buffer.from(JSON.stringify(task.plan.research_inputs)).toString("base64")] : [])]))
    if (digest(assets.assets) !== digest(task.plan.assets)) throw new ReviewRequired("Frozen source or engine changed; cannot dispatch")
    await this.call(createArgs(task.plan, stage, true))
  }
  async submit(task: Task, stage: Stage) {
    if (stage.phase === "carsidock") requireDockingEngine("carsidock-cov")
    this.bound(task)
    const result = z.object({ name: z.literal(stage.name), status: z.string() }).passthrough().parse(await this.call(createArgs(task.plan, stage, false)))
    return { handle: result.name }
  }
  async observe(task: Task, stage: Stage): Promise<Observation> {
    this.bound(task)
    const spec = task.plan.steps[stage.ordinal].resource
    try {
      const raw = await this.call([spec.kind, "status", stage.name, "--workspace", spec.workspace])
      const detail = z.object({ project: z.string().optional() }).passthrough().parse(raw)
      const normalized = detail.project && detail.project !== spec.project
        ? projectObservation(raw, spec.project, await this.call(["project", "detail", spec.project])) : raw
      return observation(normalized, stage, spec)
    } catch (error) {
      if (error instanceof CLIError && ["JobNotFound", "HPCNotFound", "NotFound"].includes(error.code)) return { state: "missing" }
      throw error
    }
  }
  async verify(task: Task, stage: Stage) {
    this.bound(task)
    try { return await this.remote(task.plan.runtime, ["verify", task.id, stage.phase, task.digest]) }
    catch (error) {
      if (error instanceof CLIError && ["remote_worker_failed", "CommandFailed"].includes(error.code)) throw new ReviewRequired("Remote verifier rejected or could not read the artifacts; inspect evidence before continuing")
      throw error // Transport timeout is not evidence of corrupt output or a stopped job.
    }
  }
  async stop(task: Task, stage: Stage) {
    this.bound(task)
    const spec = task.plan.steps[stage.ordinal].resource
    await this.call([spec.kind, "stop", stage.name, "--workspace", spec.workspace])
  }
  async failure(task: Task, stage: Stage, witness?: string) {
    this.bound(task)
    if (task.state !== "FAILED" || stage.task_id !== task.id || stage.state !== "FAILED" || stage.submitted === null ||
        stage.phase !== task.plan.steps[stage.ordinal]?.phase || stage.name !== `${task.id}-${stage.phase}` ||
        (witness !== undefined && !/^[a-f0-9]{64}$/.test(witness))) {
      throw new ReviewRequired("Failure read requires this task's reserved failed stage and exact optional witness")
    }
    return this.remote(task.plan.runtime, ["read-failure", task.id, stage.phase, task.digest, ...(witness ? [witness] : [])])
  }
  async download(task: Task, remote: string, destination: string, limit: number) {
    this.bound(task)
    const prefix = `${task.plan.runtime.root}/tasks/${task.id}/`
    if (!remote.startsWith(prefix) || /[\\\x00-\x1f\x7f]/.test(remote) ||
        remote.slice(prefix.length).split("/").some((part) => !part || part === "." || part === "..")) {
      throw new ReviewRequired("Download path is outside this task")
    }
    // The CLI itself checks Notebook transport. H100/H200 cannot SCP; do not
    // start a new Notebook or switch accounts when this file entry is unavailable.
    const script = "import os,stat,sys; p=sys.argv[1]; s=os.lstat(p); assert stat.S_ISREG(s.st_mode); assert os.path.realpath(p)==p; assert s.st_size<=int(sys.argv[2]); print(s.st_size)"
    const response = z.object({ returncode: z.number(), output: z.string() }).parse(await this.call([
      "notebook", "exec", this.config.notebook, "--workspace", this.config.workspace, "--timeout", "30",
      `python3 -I -c ${quote(script)} ${quote(remote)} ${quote(String(limit))}`,
    ]))
    if (response.returncode !== 0 || !/^\d+\s*$/.test(response.output) || Number(response.output) > limit) {
      throw new ReviewRequired("Remote artifact is not a bounded regular file")
    }
    // File-only transfer budget: retained CovDock containers can be tens of MB.
    // Keep the outer watchdog longer than SCP; scientific runtime grants and
    // the preflight size/hash boundaries remain unchanged.
    await this.call(["notebook", "scp", this.config.notebook, "--workspace", this.config.workspace,
      "--download", "--timeout", "300", remote, destination], 330000)
  }
}
