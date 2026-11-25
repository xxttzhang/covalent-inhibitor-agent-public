import { randomUUID } from "node:crypto"
import type { RefinementPlan } from "./refinement-approval"
import { RefinementRuns, type RefinementRun } from "./refinement-runs"
import { ReviewRequired, type Observation } from "./runner"
import { RefinementWorkflows, executableRefinementStages } from "./refinement-workflow"

export interface RefinementTransport {
  // Idempotent exact-plan staging only; no engines or resource allocation.
  prepare(plan: RefinementPlan, run: RefinementRun): Promise<void>
  preflight(plan: RefinementPlan, run: RefinementRun): Promise<void>
  submit(plan: RefinementPlan, run: RefinementRun): Promise<{ handle: string }>
  observe(plan: RefinementPlan, run: RefinementRun): Promise<Observation>
  stop(plan: RefinementPlan, run: RefinementRun): Promise<void>
  // Must freshly rehash actual files and parse independent engine evidence.
  verify(plan: RefinementPlan, run: RefinementRun): Promise<unknown>
}

// Persisted coordinator; the production daemon composes fixed runtime admission,
// Inspire dispatch, retained readbacks and structural QC around this runner.
export class RefinementRunner {
  readonly owner = randomUUID()
  constructor(readonly runs: RefinementRuns, readonly transport: RefinementTransport) {}
  async tickWorkflow(id: string) {
    const workflows = new RefinementWorkflows(this.runs.approvals)
    const workflow = workflows.get(id)
    const [af3, covdock] = workflow.plan.stages
    workflows.authorize(covdock.id)
    if (workflow.plan.recovery_of) workflows.verifyRetry(workflow.plan)
    this.runs.approvals.store.db.transaction(() => executableRefinementStages(workflow.plan).forEach((stage) => this.runs.ensure(stage.id)))()
    if (!workflow.plan.recovery_of) await this.tick(af3.id)
    const first = this.runs.get(af3.id)
    if (["FAILED", "CANCELLED"].includes(first.state)) this.runs.cancel(covdock.id, covdock.directory)
    // tick enforces the verified predecessor boundary and cancellation handling;
    // restarts/concurrent observers reconcile the same saved workload names.
    await this.tick(covdock.id)
    return workflow.plan.stages.map((stage) => this.runs.get(stage.id))
  }
  async tick(id: string) {
    this.runs.ensure(id) // Never creates execution for an unapproved draft.
    if (!this.runs.acquire(id, this.owner)) return
    const heartbeat = setInterval(() => this.runs.renew(id, this.owner), 10000)
    try {
      const run = this.runs.get(id)
      if (["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.state)) return
      const task = this.runs.receipt(id)
      if (Date.now() >= task.plan.finish_before && run.state !== "CANCEL_REQUESTED") {
        this.runs.cancel(id, task.plan.directory)
        this.runs.event(id, "deadline_exceeded", { finish_before: task.plan.finish_before })
      }
      if (this.runs.get(id).state === "CANCEL_REQUESTED" && run.submitted === null) {
        this.runs.update(id, this.owner, "CANCELLED")
        return
      }
      if (run.state === "WAITING" && this.runs.get(id).state !== "CANCEL_REQUESTED") {
        if (!this.runs.ready(id)) return
        await this.transport.prepare(task.plan, run)
        this.runs.owned(id, this.owner)
        if (this.runs.get(id).state === "CANCEL_REQUESTED") return
        await this.transport.preflight(task.plan, run)
        const reserved = this.runs.reserve(id, this.owner)
        // No await between the last cancellation check and the single submission.
        if (this.runs.get(id).state === "CANCEL_REQUESTED") {
          // This owner has not invoked submit yet; no remote create is in flight.
          this.runs.update(id, this.owner, "CANCELLED")
          return
        }
        try {
          const result = await this.transport.submit(task.plan, reserved)
          this.runs.update(id, this.owner, "SUBMITTED", null, result.handle)
        } catch {
          this.runs.event(id, "submission_uncertain", { name: reserved.name, instruction: "Observe exact reserved name; never replay create" })
        }
        return
      }
      if (run.submitted === null) return // A preflight review is not permission to retry.
      const observed = await this.transport.observe(task.plan, this.runs.get(id))
      const current = this.runs.owned(id, this.owner)
      if (observed.handle && (observed.handle !== current.name || (current.handle && observed.handle !== current.handle))) throw new ReviewRequired("Observed refinement workload identity changed")
      if (observed.state === "missing") {
        this.runs.event(id, "observation_unresolved", { name: current.name, instruction: "Missing observation is not termination or permission to resubmit" })
        if (current.state !== "CANCEL_REQUESTED" && Date.now() - current.submitted! > 300000) {
          this.runs.update(id, this.owner, "NEEDS_REVIEW", "Submission unresolved; manual reconciliation required")
        }
        return
      }
      if (current.state === "CANCEL_REQUESTED") {
        if (["pending", "running"].includes(observed.state)) {
          await this.transport.stop(task.plan, current)
          this.runs.event(id, "stop_requested", { name: current.name })
          return // Stop acknowledgement is not evidence of remote termination.
        }
        this.runs.update(id, this.owner, "CANCELLED", null, observed.handle)
        return
      }
      if (["pending", "running"].includes(observed.state)) {
        if (current.state !== "NEEDS_REVIEW") this.runs.update(id, this.owner, observed.state === "pending" ? "QUEUED" : "RUNNING", null, observed.handle)
        return
      }
      if (observed.state !== "succeeded") {
        this.runs.update(id, this.owner, observed.state === "failed" ? "FAILED" : "CANCELLED",
          `Authoritative ${task.plan.engine.name} terminal state: ${observed.state}`, observed.handle)
        return
      }
      if (current.state === "NEEDS_REVIEW" && !this.runs.resumeReadback(id, this.owner)) return
      this.runs.update(id, this.owner, "VERIFYING", null, observed.handle)
      const evidence = await this.transport.verify(task.plan, this.runs.get(id))
      this.runs.owned(id, this.owner)
      if (this.runs.get(id).state === "CANCEL_REQUESTED") return
      try { this.runs.complete(id, this.owner, evidence) }
      catch (error) { throw new ReviewRequired(error instanceof Error ? error.message : "Invalid refinement output") }
    } catch (error) {
      // Do not let a late network reply mutate state after another observer takes
      // the lease. A newer owner will reconcile the same persisted reservation.
      const current = this.runs.get(id)
      if (current.lease_owner !== this.owner || current.lease_until <= Date.now()) return
      if (error instanceof ReviewRequired && current.state !== "CANCEL_REQUESTED") this.runs.update(id, this.owner, "NEEDS_REVIEW", error.message)
      this.runs.event(id, "tick_error", { message: error instanceof Error ? error.message : "Unknown refinement executor error" })
    } finally { clearInterval(heartbeat); this.runs.release(id, this.owner) }
  }
}
