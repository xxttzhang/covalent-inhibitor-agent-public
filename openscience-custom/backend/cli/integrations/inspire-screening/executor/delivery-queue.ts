import z from "zod"
import { randomUUID } from "node:crypto"
import { artifact, deliverArtifact } from "./delivery"
import { digest, verifyOutcome, type Outcome, type Task } from "./model"
import { Publications, publications } from "./publication"
import { readReport } from "./report"
import { ReviewRequired } from "./runner"
import type { Store } from "./store"
import { failureInventory, readFailureReport } from "./failure-report"

const inventory = z.array(artifact.extend({ sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict())
const row = z.object({ plan_digest: z.string(), inventory_digest: z.string(), inventory: z.string(),
  state: z.enum(["RUNNING", "PAUSED", "COMPLETE"]), total_files: z.number().int().nonnegative(),
  completed_files: z.number().int().nonnegative(), total_bytes: z.number().int().nonnegative(),
  error: z.string().nullable(), retry_after: z.number().nullable(), updated: z.number() })

export function deliveryStatus(store: Store, task: Task) {
  if (!task.plan.delivery || !task.approval) return null
  if (task.state === "FAILED" && !task.plan.delivery.failure) return null
  const scope = task.state === "FAILED" ? "failure_evidence" as const : "core_results" as const
  const value = store.db.query("SELECT * FROM deliveries WHERE task_id=?").get(task.id)
  if (!value) return { scope, state: "WAITING" as const, total_files: null, completed_files: 0, total_bytes: 0, error: null, updated: null }
  const result = row.parse(value)
  if (result.plan_digest !== task.digest) throw new ReviewRequired("Delivery belongs to another approved plan")
  return { scope, state: result.state, total_files: result.inventory === "null" ? null : result.total_files, completed_files: result.completed_files,
    total_bytes: result.total_bytes, error: result.error, updated: result.updated }
}

// One bounded file per tick. This worker has no submit, cancel, reset, or model
// methods. Completion means ALL witnessed core artifacts, never a 50-file preview.
export class DeliveryQueue {
  constructor(readonly store: Store, readonly transport: Parameters<typeof deliverArtifact>[4],
    readonly publisher = new Publications(store)) {}

  entries(task: Task) {
    if (task.state === "FAILED") return inventory.parse(failureInventory(this.store, task))
    if (task.state !== "SUCCEEDED" || !task.plan.delivery || !task.approval ||
        digest(task.plan) !== task.digest || JSON.parse(task.approval).digest !== task.digest) {
      throw new ReviewRequired("Automatic delivery requires a completed task with its own approved delivery policy")
    }
    const stages = this.store.stages(task.id)
    if (stages.length !== task.plan.steps.length) throw new ReviewRequired("Delivery lost its core stages")
    const chain = stages.reduce<Outcome[]>((results, stage, index) => {
      if (stage.ordinal !== index || stage.task_id !== task.id || stage.phase !== task.plan.steps[index].phase ||
          stage.state !== "SUCCEEDED" || !stage.manifest) throw new ReviewRequired("Delivery core chain is incomplete")
      return [...results, verifyOutcome(JSON.parse(stage.manifest), task, stage, results[index - 1] ?? null)]
    }, [])
    return inventory.parse(chain.flatMap((stage) => Object.entries(stage.artifacts)
      .sort(([a], [b]) => a.localeCompare(b)).map(([name, sha256]) => ({ phase: stage.phase, name, sha256 }))))
  }

  async tick(id: string, abort = new AbortController().signal) {
    const task = this.store.get(id)
    if (!task.plan.delivery || (task.state !== "SUCCEEDED" && !(task.state === "FAILED" && task.plan.delivery.failure))) {
      return { worked: false, reason: "not_eligible" }
    }
    if (!task.approval || JSON.parse(task.approval).digest !== task.digest) throw new ReviewRequired("Delivery has no unchanged approval")
    // Reserve an empty, explicitly unknown inventory before remote reads so
    // unavailable failure evidence can pause visibly and obey the same backoff.
    this.store.db.query(`INSERT OR IGNORE INTO deliveries(task_id,plan_digest,inventory_digest,inventory,state,total_files,updated)
      VALUES(?,?,'','null','RUNNING',0,?)`).run(id, task.digest, Date.now())
    const current = row.parse(this.store.db.query("SELECT * FROM deliveries WHERE task_id=?").get(id))
    if (current.plan_digest !== task.digest) throw new ReviewRequired("Delivery belongs to another frozen plan")
    if (current.state === "COMPLETE") {
      try {
        const entries = this.entries(task)
        const identity = digest(entries)
        if (current.inventory_digest !== identity || digest(inventory.parse(JSON.parse(current.inventory))) !== identity ||
            current.total_files !== entries.length || current.completed_files !== entries.length) throw new ReviewRequired("Inventory mismatch")
      } catch {
        this.store.db.query("UPDATE deliveries SET state='PAUSED',error=?,retry_after=NULL,updated=? WHERE task_id=?")
          .run("Completed delivery inventory changed; review required", Date.now(), id)
        throw new ReviewRequired("Completed delivery inventory changed")
      }
      return { worked: false, reason: "complete" }
    }
    if (current.state === "PAUSED" && (current.retry_after === null || current.retry_after > Date.now())) {
      return { worked: false, reason: "paused" }
    }
    const owner = randomUUID()
    const lease = () => this.store.db.query("UPDATE deliveries SET lease_until=? WHERE task_id=? AND owner=?")
      .run(Date.now() + 120000, id, owner)
    const claimed = this.store.db.query("UPDATE deliveries SET owner=?,lease_until=? WHERE task_id=? AND lease_until<?")
      .run(owner, Date.now() + 120000, id, Date.now()).changes
    if (!claimed) return { worked: false, reason: "busy" }
    const heartbeat = setInterval(lease, 10000)
    try {
      abort.throwIfAborted()
      const failure = async () => {
        if (!this.transport.failure) throw new ReviewRequired("Failure readback transport unavailable")
        const result = await readFailureReport(this.store, id, { verify: this.transport.verify.bind(this.transport),
          failure: this.transport.failure.bind(this.transport) }, abort)
        if (!result.failure_evidence_ready) {
          if (result.verification === "verification_unavailable") throw new Error(result.reason)
          throw new ReviewRequired(result.reason)
        }
      }
      if (task.state === "FAILED") await failure()
      const entries = this.entries(this.store.get(id))
      const identity = digest(entries)
      this.store.db.query("UPDATE deliveries SET inventory_digest=?,inventory=?,total_files=? WHERE task_id=? AND owner=? AND inventory='null'")
        .run(identity, JSON.stringify(entries), entries.length, id, owner)
      const frozen = row.parse(this.store.db.query("SELECT * FROM deliveries WHERE task_id=?").get(id))
      if (frozen.plan_digest !== task.digest || frozen.inventory_digest !== identity || digest(inventory.parse(JSON.parse(frozen.inventory))) !== identity) {
        throw new ReviewRequired("Frozen delivery inventory changed")
      }
      if (entries.length > task.plan.delivery.max_files) throw new ReviewRequired("Complete artifact inventory exceeds approved delivery file limit; no truncation permitted")
      const receipts = publications(this.store, task)
      const prior = new Map(receipts.map((item) => [JSON.stringify([item.phase, item.name, item.sha256]), item]))
      const matched = entries.map((entry) => prior.get(JSON.stringify([entry.phase, entry.name, entry.sha256])))
      const bytes = matched.reduce((sum, item) => sum + (item?.bytes ?? 0), 0)
      if (matched.some((item) => item && item.bytes > task.plan.delivery!.max_file_bytes)) throw new ReviewRequired("Saved artifact exceeds approved per-file delivery limit")
      if (bytes > task.plan.delivery.max_total_bytes) throw new ReviewRequired("Saved files exceed approved automatic delivery byte limit")
      const next = entries.find((_, index) => !matched[index])
      if (next) {
        const file = await deliverArtifact(task, this.store.stages(id), { phase: next.phase, name: next.name },
          { directory: task.plan.directory, sessionID: task.plan.session, abort }, this.transport,
          Math.min(task.plan.delivery.max_file_bytes, task.plan.delivery.max_total_bytes - bytes), this.store)
        abort.throwIfAborted()
        await this.publisher.publish(task, file)
      }
      // Count only receipts for this exact complete inventory, including files
      // previously delivered manually. Remote changes still stop finalization.
      const saved = publications(this.store, task)
      const records = new Map(saved.map((item) => [JSON.stringify([item.phase, item.name, item.sha256]), item]))
      const verified = entries.map((entry) => records.get(JSON.stringify([entry.phase, entry.name, entry.sha256])))
      const completed = verified.filter(Boolean).length
      const total = verified.reduce((sum, item) => sum + (item?.bytes ?? 0), 0)
      if (total > task.plan.delivery.max_total_bytes) throw new ReviewRequired("Delivery total exceeds approved byte limit")
      if (completed === entries.length) {
        if (task.state === "FAILED") await failure()
        if (task.state === "SUCCEEDED") {
          const fresh = await readReport(this.store.get(id), this.store.stages(id), this.transport)
          if (!fresh.report_ready) throw new ReviewRequired("Core evidence unavailable or changed at delivery completion")
        }
      }
      abort.throwIfAborted()
      if (digest(this.entries(this.store.get(id))) !== identity) throw new ReviewRequired("Delivery task or evidence changed during transfer")
      const state = completed === entries.length ? "COMPLETE" : "RUNNING"
      const updated = this.store.db.query(`UPDATE deliveries SET state=?,completed_files=?,total_bytes=?,error=NULL,retry_after=NULL,updated=?
        WHERE task_id=? AND owner=? AND inventory_digest=?`).run(state, completed, total, Date.now(), id, owner, identity).changes
      if (updated !== 1) throw new ReviewRequired("Delivery ownership lost")
      this.store.event(id, "delivery_progress", { state, completed_files: completed, total_files: entries.length, total_bytes: total, inventory_digest: identity })
      return { worked: true, state }
    } catch (error) {
      const reason = error instanceof Error ? error.message : "Artifact delivery unavailable"
      this.store.db.query("UPDATE deliveries SET state='PAUSED',error=?,retry_after=?,updated=? WHERE task_id=? AND owner=?")
        .run(reason, error instanceof ReviewRequired || abort.aborted ? null : Date.now() + 60000, Date.now(), id, owner)
      return { worked: false, reason }
    } finally {
      clearInterval(heartbeat)
      this.store.db.query("UPDATE deliveries SET owner=NULL,lease_until=0 WHERE task_id=? AND owner=?").run(id, owner)
    }
  }

  resume(task: Task) {
    if (task.state !== "FAILED") this.entries(task)
    if (task.state === "FAILED" && (!task.plan.delivery?.failure || this.store.get(task.id).digest !== task.digest ||
        !task.approval || JSON.parse(task.approval).digest !== task.digest)) throw new ReviewRequired("Failure resume requires its unchanged approved policy")
    this.store.db.query("UPDATE deliveries SET state='RUNNING',retry_after=NULL,error=NULL,updated=? WHERE task_id=? AND plan_digest=? AND state='PAUSED'")
      .run(Date.now(), task.id, task.digest)
    return deliveryStatus(this.store, task)
  }
}
