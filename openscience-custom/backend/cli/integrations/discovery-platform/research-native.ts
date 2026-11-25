import path from "node:path"
import { realpath } from "node:fs/promises"
import type { Store } from "../inspire-screening/executor/store"
import { approve } from "../inspire-screening/executor/approval"
import { ensureDaemon, filename as nativeFilename } from "../inspire-screening/executor/host"
import { checksum, deliveryPath } from "../inspire-screening/executor/delivery"
import { deliveryStatus } from "../inspire-screening/executor/delivery-queue"
import { failureInventory } from "../inspire-screening/executor/failure-report"
import { publications } from "../inspire-screening/executor/publication"
import type { ResearchLoopStore } from "./research-loop-store"
import { researchDigest, resourceKeys, exceedsResearchLimit } from "./research-loop-contract"
import { prepareResearchExecution, readResearchExecution, type ResearchExecutionReference } from "./research-execution"

type NativeContext = Parameters<typeof approve>[2]
export type ResearchNativeInput = {
  research: ResearchLoopStore
  native: Store
  reference: ResearchExecutionReference
}

function bound(input: ResearchNativeInput) {
  const loop = input.research.get(input.reference.loop)
  const execution = readResearchExecution(input.native, input.reference)
  if (!execution) throw Error("Research native draft has not been prepared; recover the saved exact plan first")
  if (loop.digest !== input.reference.contract_digest || researchDigest(execution.prepared.contract) !== loop.digest)
    throw Error("Research native task belongs to another immutable contract")
  const reservation = input.research.reservations(loop.id).find(row => row.key === input.reference.key)
  if (!reservation || reservation.payload.kind !== "compute" || reservation.payload.plan_digest !== execution.task.digest ||
    reservation.payload.source_release !== loop.contract.inputs.source_release ||
    reservation.payload.candidate_slots !== execution.task.plan.shortlist ||
    researchDigest(reservation.payload.resources) !== researchDigest(execution.reserved_resources) ||
    (reservation.task !== null && reservation.task !== execution.task.id))
    throw Error("Native task lacks its matching cumulative research reservation")
  if (execution.task.plan.finish_before > loop.created + loop.contract.limits.max_elapsed_ms)
    throw Error("Native plan resets the original research deadline")
  return { loop, execution, reservation }
}

function active(input: ResearchNativeInput, context?: NativeContext) {
  const current = bound(input)
  if (context && (context.sessionID !== current.loop.contract.session || context.directory !== current.loop.contract.directory))
    throw Error("Native approval context belongs to another conversation or directory")
  context?.abort.throwIfAborted()
  if (["FINISHED", "CANCELLED", "BLOCKED"].includes(current.loop.state) ||
    Date.now() >= current.loop.created + current.loop.contract.limits.max_elapsed_ms ||
    current.reservation.state !== "RESERVED") throw Error("Research loop cannot authorize or start computation in its current state")
  if (resourceKeys.some(key => exceedsResearchLimit(current.loop.ledger.total[key], current.loop.contract.limits.resources[key])) ||
    input.research.reservations(current.loop.id).some(row => row.actual && resourceKeys.some(key => row.actual![key] > row.payload.resources[key] + 1e-9)))
    throw Error("Cumulative resource breach prevents native execution")
  return current
}

/** The durable claim protects the native permission request itself. A previous
 * claim with a DRAFT is uncertain, never permission to ask/submit again. Only the
 * existing exact-plan approval implementation can grant this task approval. */
export async function approveResearchNative(input: ResearchNativeInput & { context: NativeContext }) {
  const before = active(input, input.context)
  // Revalidate the exact local mapping/subset/gate proofs before opening a card.
  await prepareResearchExecution(input.native, before.execution.prepared, input.reference)
  active(input, input.context)
  input.research.bindTask(before.loop.id, input.reference.key, before.execution.task.id, before.execution.task.digest)
  const claim = input.research.claimSubmission(before.loop.id, input.reference.key,
    input.research.get(before.loop.id).revision, before.execution.task.digest)
  const prior = readResearchExecution(input.native, input.reference)!
  if (!claim.claimed || prior.task.state !== "DRAFT") return {
    state: prior.task.approval ? "approved" as const : prior.task.state === "DRAFT" ? "needs_reconciliation" as const : "not_approved" as const,
    approval_requested: false, resumed: true, task_id: prior.task.id, plan_digest: prior.task.digest,
    native_state: prior.task.state, approval: prior.task.approval, submitted_by_bridge: false,
  }
  const facts = { task_id: prior.task.id, plan_digest: prior.task.digest, contract_digest: before.loop.digest,
    reservation_key: input.reference.key, resources: prior.reserved_resources }
  input.research.record(before.loop.id, `native-approval-request:${input.reference.key}`, facts)
  input.research.transition(before.loop.id, input.research.get(before.loop.id).revision, "AWAITING_APPROVAL", facts)
  try {
    const task = await approve(input.native, prior.task.id, { ...input.context,
      ask: async request => {
        active(input, input.context)
        await input.context.ask(request)
        // A late native reply must not approve a cancelled, expired or over-budget loop.
        active(input, input.context)
      } })
    active(input, input.context)
    input.research.record(before.loop.id, `native-approval-granted:${input.reference.key}`, {
      ...facts, approval: task.approval, native_state: task.state,
    })
    return { state: "approved" as const, approval_requested: true, resumed: false,
      task_id: task.id, plan_digest: task.digest, native_state: task.state,
      approval: task.approval, submitted_by_bridge: false }
  } catch (error) {
    const task = input.native.get(prior.task.id)
    // The native approval handler closes unapproved interrupted drafts. If the
    // loop changed immediately after a valid reply, retain and cancel that exact
    // task rather than manufacturing a rejection or replacing it with a new one.
    if (task.approval && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.state)) input.native.cancel(task.id, task.plan.directory)
    const retained = input.native.get(task.id)
    const reason = error instanceof Error ? error.message : String(error)
    input.research.record(before.loop.id, `native-approval-ended:${input.reference.key}`, {
      ...facts, native_state: retained.state, reason, approval: retained.approval,
    })
    return { state: retained.approval ? "approval_interrupted" as const : "not_approved" as const, approval_requested: true, resumed: false,
      task_id: retained.id, plan_digest: retained.digest, native_state: retained.state,
      approval: retained.approval, error: reason, submitted_by_bridge: false }
  }
}

/** Call only after native exact-plan approval. Reuses the existing supervisor
 * and Runner stage journal; it never invokes a raw submit or creates another
 * task. A timeout preserves the task and tells the host to reconcile its state. */
export async function startResearchNative(input: ResearchNativeInput & { context: NativeContext }) {
  const current = active(input, input.context)
  if (!current.execution.task.approval || !["APPROVED", "ACTIVE", "NEEDS_REVIEW"].includes(current.execution.task.state))
    throw Error("Native scheduling requires this task's recorded exact-plan approval")
  if (!input.research.events(current.loop.id).some(event => {
    const payload = event.payload as { key?: string; plan_digest?: string }
    return event.kind === "submission_claimed" && payload.key === input.reference.key && payload.plan_digest === current.execution.task.digest
  })) throw Error("Native scheduling has no prior research approval claim; inspect instead of starting")
  const claim = input.research.claimSubmission(current.loop.id, input.reference.key,
    input.research.get(current.loop.id).revision, current.execution.task.digest)
  if (claim.claimed) throw Error("Native scheduling has no prior research approval claim; inspect instead of starting")
  if (current.reservation.task !== current.execution.task.id) throw Error("Native scheduling task has not been durably bound")
  // An approved plan does not authorize replacing its local scientific proofs.
  // Idempotent preparation rehashes them without approving or creating a task.
  await prepareResearchExecution(input.native, current.execution.prepared, input.reference)
  active(input, input.context)
  if (path.resolve(input.native.db.filename) !== path.resolve(nativeFilename) ||
    await realpath(input.native.db.filename) !== await realpath(nativeFilename))
    throw Error("Native daemon may only run against its configured task database")
  active(input, input.context)
  try {
    await ensureDaemon(input.native)
    active(input, input.context)
    const latest = input.native.get(current.execution.task.id)
    input.research.record(current.loop.id, `native-daemon-ready:${input.reference.key}`, {
      task_id: latest.id, plan_digest: latest.digest, schedule: "existing-native-supervisor", submission_claim: claim.submission,
    })
    return { state: "observer_ready" as const, task_id: latest.id, native_state: latest.state,
      task_running_claimed: false, raw_submit_called: false }
  } catch (error) {
    return { state: "needs_reconciliation" as const, task_id: current.execution.task.id,
      native_state: input.native.get(current.execution.task.id).state,
      error: error instanceof Error ? error.message : String(error), task_running_claimed: false, raw_submit_called: false }
  }
}

/** Actual persisted task/Results receipts plus freshly checked local bytes.
 * No remote reads, download, publication, computation, scientific evaluator,
 * usage settlement, or research state transition is performed here. */
export async function readResearchNativeReceipt(input: ResearchNativeInput) {
  const current = bound(input)
  const task = current.execution.task
  const saved = publications(input.native, task)
  const diagnostics = { error: null as string | null }
  const inventory = task.state === "FAILED" ? (() => {
    try { return failureInventory(input.native, task) }
    catch (error) { diagnostics.error = error instanceof Error ? error.message : String(error); return [] }
  })() : current.execution.stages.flatMap(stage => Object.entries(stage.manifest?.artifacts ?? {})
    .map(([name, sha256]) => ({ phase: stage.phase, name, sha256 })))
  if (new Set(saved.map(item => `${item.phase}/${item.name}`)).size !== saved.length)
    throw Error("Native Results contains duplicate artifact receipts")
  for (const receipt of saved) {
    if (!["SUCCEEDED", "FAILED"].includes(task.state) ||
      !inventory.some(item => item.phase === receipt.phase && item.name === receipt.name && item.sha256 === receipt.sha256))
      throw Error("Native Result receipt is not in the exact task's verified artifact inventory")
  }
  const files = await Promise.all(inventory.map(async item => {
    const receipt = saved.find(value => value.phase === item.phase && value.name === item.name) ?? null
    const local = await deliveryPath(task, item)
    const base = { phase: item.phase, name: item.name, expected_sha256: item.sha256, local_path: local, receipt }
    if (!receipt) return { ...base, status: "not_delivered" as const, actual_sha256: null, bytes: null, error: null }
    try {
      if (await realpath(local) !== local) throw Error("Delivered artifact path was redirected")
      const value = await checksum(local)
      const verified = value.sha256 === item.sha256 && value.sha256 === receipt.sha256 && value.bytes === receipt.bytes
      return { ...base, status: verified ? "verified" as const : "mismatch" as const,
        actual_sha256: value.sha256, bytes: value.bytes, error: verified ? null : "Delivered bytes differ from the frozen inventory or native Results receipt" }
    } catch (error) {
      return { ...base, status: (error as NodeJS.ErrnoException).code === "ENOENT" ? "missing" as const : "unavailable" as const,
        actual_sha256: null, bytes: null, error: error instanceof Error ? error.message : String(error) }
    }
  }))
  const fresh = bound(input)
  if (fresh.execution.task.digest !== task.digest || fresh.execution.task.state !== task.state ||
    researchDigest(fresh.execution.stages) !== researchDigest(current.execution.stages) ||
    researchDigest(publications(input.native, task)) !== researchDigest(saved))
    throw Error("Native task evidence changed while reading its delivered artifacts; retry this read only")
  return { schema: "drug-design-native-receipt-v1" as const, reference: input.reference,
    task_id: task.id, plan_digest: task.digest, source_release: current.loop.contract.inputs.source_release,
    contract_digest: current.loop.digest, task_state: task.state, task_error: task.error,
    approval: task.approval, plan: task.plan, stages: current.execution.stages,
    delivery: deliveryStatus(input.native, task), files, diagnostics_error: diagnostics.error,
    publication_readback: "recorded_at_delivery" as const, local_bytes_rehashed: true as const,
    remote_artifacts_rehashed: false as const, scientific_objective: "not_evaluated_by_bridge" as const,
    reserved_resources: current.execution.reserved_resources, actual_usage: null, observed_at: Date.now() }
}

export type ResearchNativeReceipt = Awaited<ReturnType<typeof readResearchNativeReceipt>>
