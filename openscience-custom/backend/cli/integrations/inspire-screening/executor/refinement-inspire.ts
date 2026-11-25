import z from "zod"
import { digest } from "./model"
import { CLIError, Inspire, observation, projectObservation, quote } from "./inspire"
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { refinementCommand } from "./refinement-command"
import { RefinementRuns, type RefinementRun } from "./refinement-runs"
import type { RefinementTransport } from "./refinement-runner"
import { ReviewRequired } from "./runner"
import { RefinementPreparation } from "./refinement-preparation"
import { RefinementReadbacks, verifyReadbackOutcome, type RefinementReadback } from "./refinement-readback"
import { evaluateRefinementQuality } from "./refinement-quality"

type Evidence = Pick<RefinementTransport, "prepare" | "preflight" | "verify">
type Check = { plan_digest: string; command_digest: string; owner: string; checked: number; attempted: number | null }

// Production composition: staging is fixed, never a model-provided callback.
// Full engine/runtime/license and scientific validators are still mandatory.
export function refinementTransport(runs: RefinementRuns, cli: Inspire, checks: {
  preflight: Evidence["preflight"];
  verify: (plan: RefinementPlan, run: RefinementRun, files: RefinementReadback) => Promise<unknown>;
}) {
  if (!checks || typeof checks.preflight !== "function" || typeof checks.verify !== "function") {
    throw new Error("Real runtime/license and scientific validators are required")
  }
  const preparation = new RefinementPreparation(runs, cli)
  const readbacks = new RefinementReadbacks(preparation)
  return new RefinementInspire(runs, cli, {
    prepare: (plan, run) => preparation.prepare(plan, run),
    async preflight(plan, run) { await preparation.read(plan, run); await checks.preflight(plan, run) },
    async verify(plan, run) {
      const files = await readbacks.read(plan, run)
      // New approved QC policies always use the fixed production classifier;
      // a callback cannot silently substitute its own passing verdict.
      const result = plan.quality_policy ? evaluateRefinementQuality(plan, runs.approvals.parent(plan), files)
        : await checks.verify(plan, run, files)
      // Quality checks can take time; outputs must still match the same retained
      // witness when the accepted outcome is returned to the execution runner.
      return verifyReadbackOutcome(result, plan, await readbacks.read(plan, run))
    },
  })
}

// CLI vectors only. Native approval, independent runtime/scientific verification,
// exact account binding and single-dispatch persistence are enforced below.
export function refinementCreateArgs(input: RefinementPlan, run: RefinementRun, dry: boolean) {
  const plan = refinementPlan.parse(input)
  if (run.id !== plan.id || run.plan_digest !== digest(plan) || run.name !== `${plan.id}-${plan.engine.name}` ||
    run.command.plan_digest !== run.plan_digest || digest(run.command) !== run.command_digest) throw new Error("Refinement command identity differs")
  const spec = plan.resource
  const [, cpus, memory] = spec.quota.split(",").map(Number)
  const common = [spec.kind, "create", "--name", run.name, "--workspace", spec.workspace, "--project", spec.project,
    "--group", spec.group, "--quota", spec.quota, "--image", spec.image, "--priority", String(spec.priority),
    "--max-time", String(plan.minutes / 60), "--no-enable-notification", "--public-path-readonly"]
  if (spec.kind === "hpc") {
    if (memory < cpus) throw new ReviewRequired("HPC quota cannot provide one GiB per requested CPU")
    return [...common, "--image-type", spec.image_type, "--instance-count", "1", "--number-of-tasks", "1",
      "--cpus-per-task", String(cpus), "--memory-per-cpu", String(Math.max(1, Math.floor(memory / cpus) - 1)),
      "--disable-hyper-threading", "--entrypoint", `srun bash -lc ${quote(run.command.shell)}`, ...(dry ? ["--dry-run"] : [])]
  }
  return [...common, "--nodes", "1", "--shm-size", String(Math.min(8, memory)), "--no-auto-fault-tolerance",
    "--command", run.command.shell, ...(dry ? ["--dry-run"] : [])]
}

// Concrete compute transport via InspireSkill, not a registered tool. Staging,
// full runtime/license checks and scientific verification remain mandatory
// dependencies, with no no-op defaults. Do not register it before providing the
// real evidence implementation; passing dependency/CPU diagnostics is insufficient.
export class RefinementInspire implements RefinementTransport {
  constructor(readonly runs: RefinementRuns, readonly cli: Inspire, readonly evidence: Evidence) {
    if (!evidence || [evidence.prepare, evidence.preflight, evidence.verify].some((value) => typeof value !== "function")) {
      throw new Error("Independent staging, runtime and scientific evidence implementations are mandatory")
    }
    runs.approvals.store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_dispatch(
      id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, command_digest TEXT NOT NULL,
      owner TEXT NOT NULL, checked INTEGER NOT NULL, attempted INTEGER)`)
  }
  bound(plan: RefinementPlan, input: RefinementRun) {
    const task = this.runs.receipt(plan.id)
    if (task.digest !== digest(plan) || input.id !== plan.id || !input.lease_owner) throw new ReviewRequired("Refinement invocation differs from approved task")
    const run = this.runs.owned(plan.id, input.lease_owner)
    if (input.command_digest !== run.command_digest || input.name !== run.name) throw new ReviewRequired("Refinement invocation changed its reserved command")
    const parent = this.runs.approvals.store.get(plan.parent_id)
    if (parent.digest !== plan.parent_digest || parent.plan.session !== plan.session || parent.plan.directory !== plan.directory) {
      throw new ReviewRequired("Refinement parent/account identity changed")
    }
    this.cli.bound(parent)
    return { run, parent }
  }
  waiting(plan: RefinementPlan, input: RefinementRun) {
    const bound = this.bound(plan, input)
    if (bound.run.state !== "WAITING" || bound.run.submitted !== null || Date.now() >= plan.finish_before) {
      throw new ReviewRequired("Only unsubmitted in-deadline refinement can be prepared")
    }
    this.runs.approvals.parent(plan)
    if (digest(refinementCommand(plan, bound.parent)) !== bound.run.command_digest) throw new ReviewRequired("Frozen refinement command changed")
    return bound.run
  }
  async prepare(plan: RefinementPlan, input: RefinementRun) {
    const run = this.waiting(plan, input)
    await this.evidence.prepare(plan, run)
    this.waiting(plan, input)
  }
  async preflight(plan: RefinementPlan, input: RefinementRun) {
    this.waiting(plan, input)
    await this.evidence.preflight(plan, input)
    this.waiting(plan, input)
    const price = await this.cli.live(plan.resource)
    if (price !== plan.resource.points_per_hour) throw new ReviewRequired("Live refinement price changed; new scoped approval required")
    const run = this.waiting(plan, input)
    await this.cli.call(refinementCreateArgs(plan, run, true))
    this.runs.approvals.store.db.transaction(() => {
      const current = this.waiting(plan, input)
      const previous = this.check(plan.id)
      if (previous?.attempted !== null && previous?.attempted !== undefined) throw new ReviewRequired("Refinement dispatch already attempted; never resubmit")
      this.runs.approvals.store.db.query(`INSERT INTO refinement_dispatch(id,plan_digest,command_digest,owner,checked)
        VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET plan_digest=excluded.plan_digest,
        command_digest=excluded.command_digest,owner=excluded.owner,checked=excluded.checked`)
        .run(plan.id, digest(plan), current.command_digest, current.lease_owner!, Date.now())
    })()
  }
  check(id: string) {
    return this.runs.approvals.store.db.query("SELECT * FROM refinement_dispatch WHERE id=?").get(id) as Check | null
  }
  async submit(plan: RefinementPlan, input: RefinementRun) {
    // Mark the attempt before network I/O. A second transport/process cannot
    // replay create even if the first process never received a response.
    const args = this.runs.approvals.store.db.transaction(() => {
      const { run, parent } = this.bound(plan, input)
      const checked = this.check(plan.id)
      if (run.state !== "DISPATCHING" || run.submitted === null || Date.now() >= plan.finish_before ||
        !checked || checked.attempted !== null || checked.plan_digest !== digest(plan) ||
        checked.command_digest !== run.command_digest || checked.owner !== run.lease_owner ||
        Date.now() - checked.checked > 180000 || digest(refinementCommand(plan, parent)) !== run.command_digest) {
        throw new ReviewRequired("Refinement lacks a fresh owned preflight and unique dispatch reservation")
      }
      this.runs.approvals.parent(plan)
      const args = refinementCreateArgs(plan, run, false)
      this.runs.approvals.store.db.query("UPDATE refinement_dispatch SET attempted=? WHERE id=? AND attempted IS NULL")
        .run(Date.now(), plan.id)
      this.runs.event(plan.id, "cli_dispatch_attempted", { name: run.name, command_digest: run.command_digest })
      return args
    })()
    const result = z.object({ name: z.literal(input.name), status: z.string() }).passthrough().parse(await this.cli.call(args))
    return { handle: result.name }
  }
  async observe(plan: RefinementPlan, input: RefinementRun) {
    const { run } = this.bound(plan, input)
    if (run.submitted === null) throw new Error("Unsubmitted refinement has no remote workload to observe")
    const spec = plan.resource
    try {
      const raw = await this.cli.call([spec.kind, "status", run.name, "--workspace", spec.workspace])
      const detail = z.object({ project: z.string().min(1), compute_group: z.string().min(1),
        resource: z.object({ gpu: z.number(), cpu: z.number(), memory_gib: z.number(), nodes: z.number() }),
      }).passthrough().parse(raw)
      const normalized = detail.project !== spec.project
        ? projectObservation(raw, spec.project, await this.cli.call(["project", "detail", spec.project])) : raw
      this.bound(plan, input)
      return observation(normalized, { name: run.name }, spec)
    } catch (error) {
      if (error instanceof CLIError && ["JobNotFound", "HPCNotFound", "NotFound"].includes(error.code)) return { state: "missing" as const }
      throw error // Timeout/invalid schema/ambiguous identity is not termination.
    }
  }
  async stop(plan: RefinementPlan, input: RefinementRun) {
    if (this.bound(plan, input).run.state !== "CANCEL_REQUESTED") throw new Error("No recorded refinement cancellation intent")
    const observed = await this.observe(plan, input)
    if (!["pending", "running"].includes(observed.state)) return
    const { run } = this.bound(plan, input)
    if (run.state !== "CANCEL_REQUESTED") throw new Error("Cancellation intent changed")
    await this.cli.call([plan.resource.kind, "stop", run.name, "--workspace", plan.resource.workspace])
  }
  async verify(plan: RefinementPlan, input: RefinementRun) {
    const { run } = this.bound(plan, input)
    if (run.state !== "VERIFYING" || run.handle !== run.name) throw new Error("Refinement is not ready for scientific output verification")
    const result = await this.evidence.verify(plan, run)
    this.bound(plan, input)
    return result // RefinementRuns.complete still validates the final result.
  }
}
