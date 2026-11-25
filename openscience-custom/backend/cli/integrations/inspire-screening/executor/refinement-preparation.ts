import z from "zod"
import { createHash } from "node:crypto"
import { canonical, digest, type Task } from "./model"
import { Inspire, quote } from "./inspire"
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { RefinementRuns, type RefinementRun } from "./refinement-runs"
import { refinementCommand } from "./refinement-command"
import { ReviewRequired } from "./runner"
import bootstrap from "./stage_bootstrap.py" with { type: "text" }
import revisionController from "./readback_revision.py" with { type: "text" }
import { readbackRevision } from "./readback-revision"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const receipt = z.object({ version: z.literal("refinement-staging-v1"), id: z.string(), digest: hash,
  code: z.object({ release_id: hash, manifest_sha256: hash }).strict(), selection_id: hash,
  selection_manifest_digest: hash, input_count: z.number().int().positive(), path: z.string(), output: z.string(),
  runtime_verified: z.literal(false), resource_approval_granted: z.literal(false), staged: z.literal(true), plan_sha256: hash }).strict()

export function verifyPreparation(value: unknown, plan: RefinementPlan, parent: Task) {
  const parsed = refinementPlan.parse(plan)
  const result = receipt.parse(value)
  const command = refinementCommand(parsed, parent)
  const file = `${parent.plan.runtime.root}/tasks/${plan.parent_id}/refinements/${plan.id}/plan.json`
  const checksum = createHash("sha256").update(JSON.stringify(canonical({ plan: parsed, digest: digest(parsed) })) + "\n").digest("hex")
  if (result.id !== plan.id || result.digest !== digest(plan) || digest(result.code) !== digest(plan.code) ||
      result.selection_id !== plan.selection_id || result.selection_manifest_digest !== plan.selection_manifest_digest ||
      result.input_count !== plan.input.candidates.length || result.path !== file || result.output !== command.output ||
      result.plan_sha256 !== checksum) throw new ReviewRequired("Prepared remote plan differs from independent approval")
  return result
}

export async function preparationArgs(plan: RefinementPlan, parent: Task, action: "freeze" | "read" | "outputs", witness: string | null = null) {
  refinementCommand(plan, parent) // Validate complete schema, source, scope and required file roles.
  if (witness !== null) hash.parse(witness)
  if (action !== "outputs" && witness !== null) throw new ReviewRequired("Only output reads accept a retained evidence checksum")
  const root = parent.plan.runtime.root
  const release = `${root}/releases/${plan.code.release_id}`
  const revision = action === "outputs" ? readbackRevision(plan, root) : null
  const packet = { root, release: plan.code.release_id, manifest: plan.code.manifest_sha256,
    checker_sha256: plan.assets[`${release}/code_release.py`], python: plan.runtime.python,
    python_sha256: plan.assets[plan.runtime.python], action, digest: digest(plan), parent: plan.parent_id, task: plan.id,
    payload: action === "freeze" ? Buffer.from(JSON.stringify(plan)).toString("base64") : null,
    ...(action === "outputs" ? { evidence_sha256: witness } : {}),
    ...(revision ? { reader_revision: revision, revision_controller: Buffer.from(revisionController).toString("base64") } : {}) }
  const encoded = Buffer.from(JSON.stringify(packet)).toString("base64")
  if (encoded.length > 2 * 1024 * 1024) throw new ReviewRequired("Frozen staging packet exceeds bounded transport size")
  // Build-time text import keeps the trusted verifier in the versioned host
  // bundle. Deployment must not depend on a mutable neighbouring source file.
  const command = ["env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
    "python3", "-I", "-B", "-c", bootstrap, encoded].map(quote).join(" ")
  return ["notebook", "exec", parent.plan.connection.notebook, "--workspace", parent.plan.connection.workspace,
    "--timeout", "90", command]
}

// Connects already-approved, leased executions to CPU Notebook file preparation.
// No resource creation, approval UI, raw model command, or old-worker mutation.
export class RefinementPreparation {
  constructor(readonly runs: RefinementRuns, readonly cli: Inspire) {
    runs.approvals.store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_preparation(
      id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, receipt TEXT NOT NULL, updated INTEGER NOT NULL)`)
  }
  bound(plan: RefinementPlan, input: RefinementRun, writing = false) {
    const task = this.runs.receipt(plan.id)
    if (task.digest !== digest(plan) || input.id !== plan.id || !input.lease_owner) throw new ReviewRequired("Unapproved or unowned refinement preparation")
    const run = this.runs.owned(plan.id, input.lease_owner)
    if (writing && (run.state !== "WAITING" || run.submitted !== null || Date.now() >= plan.finish_before)) {
      throw new ReviewRequired("Only unsubmitted approved refinement may be prepared")
    }
    const parent = this.runs.approvals.parent(plan)
    this.cli.bound(parent)
    if (digest(refinementCommand(plan, parent)) !== run.command_digest) throw new ReviewRequired("Preparation command differs from frozen execution")
    return parent
  }
  async invoke(plan: RefinementPlan, input: RefinementRun, action: "freeze" | "read") {
    const parent = this.bound(plan, input, action === "freeze")
    const args = await preparationArgs(plan, parent, action)
    this.bound(plan, input, action === "freeze")
    const result = z.object({ returncode: z.number(), output: z.string().max(64 * 1024) }).parse(await this.cli.call(args))
    this.bound(plan, input, action === "freeze")
    if (result.returncode !== 0) throw new ReviewRequired("Remote staging verifier rejected the frozen plan; inspect evidence")
    return verifyPreparation(JSON.parse(result.output), plan, parent)
  }
  saved(plan: RefinementPlan) {
    const row = this.runs.approvals.store.db.query("SELECT plan_digest,receipt FROM refinement_preparation WHERE id=?").get(plan.id) as
      { plan_digest: string; receipt: string } | null
    if (row && row.plan_digest !== digest(plan)) throw new ReviewRequired("Saved preparation belongs to another plan")
    return row ? receipt.parse(JSON.parse(row.receipt)) : null
  }
  async prepare(plan: RefinementPlan, input: RefinementRun) {
    this.bound(plan, input, true)
    const frozen = await this.invoke(plan, input, this.saved(plan) ? "read" : "freeze")
    const fresh = await this.invoke(plan, input, "read")
    if (digest(frozen) !== digest(fresh)) throw new ReviewRequired("Remote preparation changed during readback")
    this.runs.approvals.store.db.transaction(() => {
      this.bound(plan, input, true)
      const saved = this.saved(plan)
      if (saved && digest(saved) !== digest(fresh)) throw new ReviewRequired("Remote preparation differs from retained receipt")
      this.runs.approvals.store.db.query("INSERT OR IGNORE INTO refinement_preparation(id,plan_digest,receipt,updated) VALUES(?,?,?,?)")
        .run(plan.id, digest(plan), JSON.stringify(fresh), Date.now())
    })()
  }
  async read(plan: RefinementPlan, input: RefinementRun) {
    this.bound(plan, input)
    const saved = this.saved(plan)
    if (!saved) throw new ReviewRequired("No independently retained preparation receipt")
    const fresh = await this.invoke(plan, input, "read")
    if (digest(saved) !== digest(fresh)) throw new ReviewRequired("Prepared files changed since the original receipt")
    return fresh
  }
}
