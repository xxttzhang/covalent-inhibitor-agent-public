import { digest } from "./model"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"
import { refinementCommand } from "./refinement-command"
import { verifyRefinementResult } from "./refinement-result"
import { ReviewRequired } from "./runner"
import { readbackRevision } from "./readback-revision"

export type RefinementState = "WAITING" | "DISPATCHING" | "SUBMITTED" | "QUEUED" | "RUNNING" | "VERIFYING" | "NEEDS_REVIEW" |
  "CANCEL_REQUESTED" | "CANCELLED" | "FAILED" | "SUCCEEDED"
export type RefinementRun = { id: string; plan_digest: string; name: string; state: RefinementState;
  command: ReturnType<typeof refinementCommand>; command_digest: string; handle: string | null;
  submitted: number | null; manifest: string | null; error: string | null; updated: number;
  lease_owner: string | null; lease_until: number }
const terminal = ["FAILED", "CANCELLED", "SUCCEEDED"]

export class RefinementRuns {
  constructor(readonly approvals: Refinements) {
    approvals.store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_runs(
      id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, name TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
      command TEXT NOT NULL, command_digest TEXT NOT NULL, handle TEXT, submitted INTEGER,
      manifest TEXT, error TEXT, updated INTEGER NOT NULL, lease_owner TEXT, lease_until INTEGER NOT NULL DEFAULT 0)`)
  }
  get(id: string): RefinementRun {
    const row = this.approvals.store.db.query("SELECT * FROM refinement_runs WHERE id=?").get(id) as
      (Omit<RefinementRun, "command"> & { command: string }) | null
    if (!row) throw new Error("Unknown refinement execution")
    const task = this.approvals.get(id)
    const command = JSON.parse(row.command) as RefinementRun["command"]
    if (row.plan_digest !== task.digest || row.name !== `${task.id}-${task.plan.engine.name}` ||
      command.plan_digest !== task.digest || digest(command) !== row.command_digest) throw new Error("Immutable refinement execution changed")
    return { ...row, command }
  }
  list() {
    return (this.approvals.store.db.query("SELECT id FROM refinement_runs ORDER BY updated,id").all() as { id: string }[])
      .map((row) => this.get(row.id))
  }
  receipt(id: string) {
    const task = this.approvals.get(id)
    if (this.approvals.workflow(id) || task.state === "WORKFLOW_APPROVED") {
      new RefinementWorkflows(this.approvals).authorize(id)
      return task
    }
    const receipt = task.approval ? JSON.parse(task.approval) : null
    if (task.state !== "APPROVED" || !receipt || receipt.digest !== task.digest || receipt.session !== task.plan.session ||
      receipt.boundary !== "remote_compute" || typeof receipt.nonce !== "string" || !/^[a-f0-9]{32}$/.test(receipt.nonce) ||
      !Number.isSafeInteger(receipt.at) || receipt.at < task.plan.created || receipt.at > task.plan.approve_before) {
      throw new Error("No valid independent exact-plan refinement approval")
    }
    return task
  }
  ready(id: string) {
    this.receipt(id)
    return !this.approvals.workflow(id) || new RefinementWorkflows(this.approvals).ready(id)
  }
  ensure(id: string) {
    return this.approvals.store.db.transaction(() => {
      const task = this.receipt(id)
      if (this.approvals.store.db.query("SELECT id FROM refinement_runs WHERE id=?").get(id)) return this.get(id)
      const parent = this.approvals.parent(task.plan)
      const command = refinementCommand(task.plan, parent)
      this.approvals.store.db.query("INSERT INTO refinement_runs(id,plan_digest,name,state,command,command_digest,updated) VALUES(?,?,?,'WAITING',?,?,?)")
        .run(id, task.digest, `${id}-${task.plan.engine.name}`, JSON.stringify(command), digest(command), Date.now())
      this.event(id, "execution_created", {})
      return this.get(id)
    })()
  }
  event(id: string, kind: string, payload: Record<string, unknown>) {
    this.approvals.store.event(this.approvals.get(id).plan.parent_id, `refinement_${kind}`, { ...payload, refinement_id: id })
  }
  resumeReadback(id: string, owner: string) {
    // Called only after transport has freshly reconciled the exact workload as
    // succeeded. The normal verify/complete path still checks every artifact.
    return this.approvals.store.db.transaction(() => {
      const run = this.owned(id, owner)
      const task = this.receipt(id)
      const parent = this.approvals.parent(task.plan)
      const revision = readbackRevision(task.plan, parent.plan.runtime.root)
      if (!revision || run.state !== "NEEDS_REVIEW" || run.submitted === null || run.handle !== run.name ||
          run.error !== "Remote refinement output verification failed; scheduler success is not scientific completion. Inspect retained engine evidence; do not resubmit.") return false
      const previous = this.approvals.store.db.query(`SELECT 1 FROM events WHERE task_id=?
        AND kind='refinement_reader_revision_resumed' AND json_extract(payload,'$.refinement_id')=?
        AND json_extract(payload,'$.reader_revision.sha256')=? LIMIT 1`).get(parent.id, id, revision.sha256)
      if (previous) return false // No automatic retry loop on a failed repair.
      this.event(id, "reader_revision_resumed", { reader_revision: revision, compute_submitted: false })
      this.update(id, owner, "VERIFYING", null, run.handle)
      return true
    })()
  }
  acquire(id: string, owner: string, now = Date.now()) {
    return this.approvals.store.db.query("UPDATE refinement_runs SET lease_owner=?,lease_until=? WHERE id=? AND (lease_until<? OR lease_owner=?)")
      .run(owner, now + 180000, id, now, owner).changes === 1
  }
  release(id: string, owner: string) {
    this.approvals.store.db.query("UPDATE refinement_runs SET lease_owner=NULL,lease_until=0 WHERE id=? AND lease_owner=?").run(id, owner)
  }
  renew(id: string, owner: string) {
    const now = Date.now()
    return this.approvals.store.db.query("UPDATE refinement_runs SET lease_until=? WHERE id=? AND lease_owner=? AND lease_until>?")
      .run(now + 180000, id, owner, now).changes === 1
  }
  owned(id: string, owner: string) {
    const run = this.get(id)
    if (run.lease_owner !== owner || run.lease_until <= Date.now()) throw new Error("Refinement execution lease lost; do not mutate or submit")
    return run
  }
  reserve(id: string, owner: string) {
    return this.approvals.store.db.transaction(() => {
      const run = this.owned(id, owner)
      const task = this.receipt(id)
      const parent = this.approvals.parent(task.plan)
      if (!this.ready(id)) throw new Error("CovDock must wait for verified AF3 completion in the approved workflow")
      if (run.state !== "WAITING" || run.submitted !== null || Date.now() >= task.plan.finish_before) throw new Error("Refinement cannot reserve after cancellation/deadline or repeat submission")
      if (digest(refinementCommand(task.plan, parent)) !== run.command_digest) throw new ReviewRequired("Refinement command changed since execution was frozen")
      this.approvals.store.db.query("UPDATE refinement_runs SET state='DISPATCHING',submitted=?,updated=?,lease_until=? WHERE id=?")
        .run(Date.now(), Date.now(), Date.now() + 180000, id)
      this.event(id, "dispatch_reserved", { name: run.name, digest: task.digest, command_digest: run.command_digest })
      return this.get(id)
    })()
  }
  // Cancellation may arrive during a network call; no terminal state is asserted
  // here for submitted jobs. The runner must observe the actual remote terminal.
  cancel(id: string, directory: string) {
    return this.approvals.store.db.transaction(() => {
      const task = this.approvals.get(id)
      if (task.plan.directory !== directory) throw new Error("Refinement belongs to another project")
      const run = this.get(id)
      if (terminal.includes(run.state) || run.state === "CANCEL_REQUESTED") return run
      this.approvals.store.db.query("UPDATE refinement_runs SET state='CANCEL_REQUESTED',updated=? WHERE id=?").run(Date.now(), id)
      this.event(id, "cancel_requested", { name: run.name })
      return this.get(id)
    })()
  }
  update(id: string, owner: string, state: Exclude<RefinementState, "SUCCEEDED" | "WAITING" | "DISPATCHING">, error: string | null = null, handle?: string) {
    return this.approvals.store.db.transaction(() => {
      const run = this.owned(id, owner)
      if (["SUCCEEDED", "WAITING", "DISPATCHING"].includes(state)) throw new Error("Use verified completion or the single reservation boundary")
      if (terminal.includes(run.state)) throw new Error("Terminal refinement execution cannot be reset")
      if (["SUBMITTED", "QUEUED", "RUNNING", "VERIFYING"].includes(state) && run.submitted === null) throw new Error("Unsubmitted refinement cannot report a submitted, queued, running or verifying state")
      if (run.state === "CANCEL_REQUESTED" && state !== "CANCELLED") return run
      if (handle && (handle !== run.name || (run.handle && handle !== run.handle))) throw new Error("Observed refinement handle changed")
      this.approvals.store.db.query("UPDATE refinement_runs SET state=?,error=?,handle=?,updated=? WHERE id=?")
        .run(state, error, handle ?? run.handle, Date.now(), id)
      this.event(id, "execution_state", { state, error, ...(handle ? { handle } : {}) })
      return this.get(id)
    })()
  }
  complete(id: string, owner: string, evidence: unknown) {
    return this.approvals.store.db.transaction(() => {
      const run = this.owned(id, owner)
      if (run.state !== "VERIFYING" || run.submitted === null || run.handle !== run.name) throw new Error("Refinement is not awaiting output verification for a reconciled workload")
      const task = this.receipt(id)
      this.approvals.parent(task.plan)
      if (Date.now() >= task.plan.finish_before) throw new Error("Refinement deadline elapsed during verification")
      const result = verifyRefinementResult(evidence, task.plan)
      this.approvals.store.db.query("UPDATE refinement_runs SET state='SUCCEEDED',manifest=?,error=NULL,updated=? WHERE id=?")
        .run(JSON.stringify(result), Date.now(), id)
      this.event(id, result.version === "screening-refinement-outcome-v2" ? "quality_evaluated" : "scientifically_verified",
        { manifest_digest: digest(result), ...(result.version === "screening-refinement-outcome-v2"
          ? { engine_success_count: result.success_count, engine_failed_count: result.failed_count,
              quality_counts: result.quality_counts, hits_confirmed: false } : {}) })
      return this.get(id)
    })()
  }
}
