import { dockingPolicyMessage } from "../../../src/session/docking-policy"
import { randomUUID } from "node:crypto"
import { digest, verifyOutcome, type Outcome, type Task, type Stage } from "./model"
import { Store } from "./store"

export type Observation = { state: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "missing"; handle?: string }
export class ReviewRequired extends Error {}
export interface Transport {
  // prepare is idempotent and may only stage the exact approved plan, never run science.
  prepare(task: Task): Promise<void>
  preflight(task: Task, stage: Stage): Promise<void>
  submit(task: Task, stage: Stage): Promise<{ handle: string }>
  observe(task: Task, stage: Stage): Promise<Observation>
  verify(task: Task, stage: Stage): Promise<unknown>
  stop(task: Task, stage: Stage): Promise<void>
}

export class Runner {
  readonly owner = randomUUID()
  constructor(readonly store: Store, readonly transport: Transport) {}
  async tick(id: string) {
    if (!this.store.acquire(id, this.owner)) return
    try {
      const task = this.store.get(id)
      if (["DRAFT", "SUCCEEDED", "FAILED", "CANCELLED"].includes(task.state)) return
      if (task.state === "CANCEL_REQUESTED" && this.store.stages(id).every((stage) => stage.state === "WAITING")) {
        this.store.state(id, "CANCELLED")
        return
      }
      if (!task.approval || JSON.parse(task.approval).digest !== task.digest) throw new Error("Task has no valid exact-plan approval")
      if (task.state !== "CANCEL_REQUESTED" && Date.now() > task.plan.finish_before) {
        this.store.cancel(id, task.plan.directory)
        this.store.event(id, "deadline_exceeded", { finish_before: task.plan.finish_before })
      }
      const stages = this.store.stages(id)
      const stage = stages.find((item) => item.state !== "SUCCEEDED")
      if (!stage) {
        if (this.store.get(id).state !== "CANCEL_REQUESTED") this.store.state(id, "SUCCEEDED")
        else this.store.state(id, "CANCELLED")
        return
      }
      if (this.store.get(id).state === "CANCEL_REQUESTED") {
        if (stage.state === "WAITING") {
          this.store.stage(id, stage.ordinal, { state: "CANCELLED" })
          this.store.state(id, "CANCELLED")
          return
        }
        const observed = await this.transport.observe(task, stage)
        if (["pending", "running"].includes(observed.state)) {
          await this.transport.stop(task, stage)
          this.store.event(id, "stop_requested", { name: stage.name })
          return // Need actual terminal observation, not the stop response.
        }
        if (observed.state === "missing") {
          this.store.event(id, "cancel_awaiting_reconciliation", { name: stage.name })
          return // An ambiguous create can still appear. Never claim it stopped.
        }
        this.store.stage(id, stage.ordinal, { state: "CANCELLED" })
        this.store.state(id, "CANCELLED")
        return
      }
      if (stage.state === "WAITING") {
        if (task.state === "NEEDS_REVIEW") return
        if (stage.phase === "carsidock") {
          this.store.state(id, "NEEDS_REVIEW", dockingPolicyMessage)
          this.store.event(id, "docking_engine_disabled", { phase: stage.phase, compute_submitted: false })
          return
        }
        // Safe staging/reads can be retried; create is reserved before any network send.
        await this.transport.prepare(task)
        await this.transport.preflight(task, stage)
        this.store.claim(id, stage.ordinal, this.owner)
        if (this.store.get(id).state === "CANCEL_REQUESTED") {
          this.store.stage(id, stage.ordinal, { state: "CANCELLED" })
          this.store.state(id, "CANCELLED")
          return
        }
        try {
          const response = await this.transport.submit(task, stage)
          this.store.stage(id, stage.ordinal, { state: "SUBMITTED", handle: response.handle })
        } catch {
          this.store.event(id, "submission_uncertain", { name: stage.name, instruction: "Observe this exact name; NEVER replay create" })
        }
        return
      }
      if (task.state === "NEEDS_REVIEW" && stage.state === "VERIFYING") return
      if (["FAILED", "CANCELLED"].includes(stage.state)) {
        this.store.state(id, stage.state === "FAILED" ? "FAILED" : "CANCELLED", stage.error)
        return
      }
      const observed = await this.transport.observe(task, stage)
      if (observed.state === "missing") {
        this.store.event(id, "job_not_found", { name: stage.name, instruction: "No automatic resubmission" })
        if (stage.submitted && Date.now() - stage.submitted > 5 * 60000 && this.store.get(id).state !== "CANCEL_REQUESTED") {
          this.store.state(id, "NEEDS_REVIEW", "Submission is unresolved; manual reconciliation required, no retry")
        }
        return
      }
      if (observed.handle && stage.handle && observed.handle !== stage.handle) throw new Error("Remote handle changed for the reserved name")
      if (["pending", "running"].includes(observed.state)) {
        this.store.stage(id, stage.ordinal, { state: observed.state === "pending" ? "QUEUED" : "RUNNING", ...(observed.handle ? { handle: observed.handle } : {}) })
        return
      }
      if (observed.state !== "succeeded") {
        const reason = `${stage.phase}: authoritative remote terminal state ${observed.state}; downstream phases were not started`
        this.store.stage(id, stage.ordinal, { state: observed.state === "failed" ? "FAILED" : "CANCELLED", error: reason })
        if (this.store.get(id).state !== "CANCEL_REQUESTED") this.store.state(id, observed.state === "failed" ? "FAILED" : "CANCELLED", reason)
        return
      }
      this.store.stage(id, stage.ordinal, { state: "VERIFYING" })
      // Transport rehashes outputs on the shared disk. A saved success flag alone is insufficient.
      const evidence = await this.transport.verify(task, stage)
      const parent = stage.ordinal ? JSON.parse(stages[stage.ordinal - 1].manifest!) as Outcome : null
      const result = (() => {
        try { return verifyOutcome(evidence, task, stage, parent) }
        catch (error) {
          const message = error instanceof Error ? error.message : "Invalid scientific output"
          this.store.stage(id, stage.ordinal, { state: "FAILED", error: message })
          if (this.store.get(id).state !== "CANCEL_REQUESTED") this.store.state(id, "FAILED", message)
          throw error
        }
      })()
      this.store.stage(id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(result), error: null })
      this.store.event(id, "scientifically_verified", { phase: stage.phase, digest: digest(result) })
      // Advance on the next tick; cancellation/approval/deadline will be read again.
    } catch (error) {
      if (error instanceof ReviewRequired && this.store.get(id).state !== "CANCEL_REQUESTED") {
        this.store.state(id, "NEEDS_REVIEW", error.message)
      }
      // Observation failures are not remote terminal states and never trigger resubmission.
      this.store.event(id, "tick_error", { message: error instanceof Error ? error.message : "Unknown executor error" })
    } finally {
      this.store.release(id, this.owner)
    }
  }
}
