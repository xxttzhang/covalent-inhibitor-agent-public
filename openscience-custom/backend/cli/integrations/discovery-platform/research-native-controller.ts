import z from "zod"
import type { Store } from "../inspire-screening/executor/store"
import { researchDigest, researchHash, resourceKeys, exceedsResearchLimit } from "./research-loop-contract"
import { ResearchDecision } from "./research-decision"
import type { ResearchLoopStore } from "./research-loop-store"
import { preparedResearchExecution, prepareResearchExecution, readResearchExecution, verifyPreparedResearchExecution,
  type PreparedResearchExecution } from "./research-execution"
import { approveResearchNative, startResearchNative, readResearchNativeReceipt } from "./research-native"
import { prepareResearchPriorUsage } from "./research-prior-usage"

export const nativeResearchCapability = "research.execute-native-plan.v1"
const registeredPlan = z.object({
  schema: z.literal("drug-design-native-plan-offer-v1"), loop_id: z.string().min(1), key: z.string().min(1),
  round: z.number().int().min(1).max(2), prepared: preparedResearchExecution,
}).strict()
const selection = z.object({
  schema: z.literal("drug-design-native-plan-selection-v1"), decision_id: z.string().min(1),
  offer_ref: z.string().min(1), key: z.string().min(1), task_id: z.string().min(1), plan_digest: researchHash,
}).strict()
const offerRef = (key: string) => `native-plan:${key}`
const selectionRef = (id: string) => `native-selection:${id}`
const rejectionRef = (id: string) => `native-permission-rejection:${id}`
const declinedRef = (id: string) => `native-approval-declined:${id}`
const rejection = z.object({ schema: z.literal("drug-design-native-permission-rejection-v1"),
  loop_id: z.string(), decision_id: z.string(), task_id: z.string(), plan_digest: researchHash, session_id: z.string(),
  permission: z.literal("remote_compute"), kind: z.enum(["rejected", "corrected"]), request_patterns: z.array(researchHash).length(1) }).strict()
const declined = z.object({ schema: z.literal("drug-design-native-approval-declined-v1"), loop_id: z.string(),
  decision_id: z.string(), task_id: z.string(), plan_digest: researchHash, reservation_key: z.string(), rejection_ref: z.string(),
  native_state: z.literal("CANCELLED"), approval: z.null(), all_stages_unsubmitted: z.literal(true),
  approval_requested_sequence: z.number().int(), approval_ended_sequence: z.number().int(),
  actual_usage: z.null(), accounting_complete: z.literal(false), reservation_released: z.literal(false) }).strict()
type Identity = { research: ResearchLoopStore; native: Store; id: string; decision_id?: string }
type NativeContext = Parameters<typeof approveResearchNative>[0]["context"]

/** A host registers an independently verified exact plan without spending its
 * compute budget or creating an approval. The model sees only its identity. */
export async function registerResearchNativePlan(input: {
  research: ResearchLoopStore; id: string; key: string; round: number; prepared: PreparedResearchExecution
}) {
  if (!input.key.trim()) throw Error("Native plan requires a stable host key")
  const prepared = await verifyPreparedResearchExecution(input.prepared)
  const priorUsage = await prepareResearchPriorUsage(prepared.contract)
  const offer = registeredPlan.parse({ schema: "drug-design-native-plan-offer-v1", loop_id: input.id,
    key: input.key, round: input.round, prepared })
  return input.research.db.transaction(() => {
    const loop = input.research.get(input.id)
    priorUsage?.verify(input.research, input.id)
    if (loop.contract.mode !== "native-compute" || prepared.contract_digest !== loop.digest ||
      researchDigest(prepared.contract) !== loop.digest || prepared.plan.finish_before > loop.created + loop.contract.limits.max_elapsed_ms)
      throw Error("Native plan does not belong to this immutable research contract and deadline")
    const previous = input.research.evidence(input.id, "host").find(row => row.key === offerRef(input.key))
    if (previous) {
      if (researchDigest(previous.payload) !== researchDigest(offer)) throw Error("Native plan key already binds another exact plan")
      return { evidence_ref: previous.key, plan_id: prepared.plan.id, registered: false }
    }
    if (resourceKeys.some(key => exceedsResearchLimit(loop.ledger.total[key] + prepared.resources[key], loop.contract.limits.resources[key])))
      throw Error("Cumulative research budget cannot cover the offered native plan")
    if (!["READY", "DECIDING"].includes(loop.state) || Date.now() >= prepared.plan.approve_before)
      throw Error("Research loop or native plan is not ready for registration")
    // Current 20k-prefix selector cannot exclude previously evaluated identities.
    if (input.round !== 1 || input.research.reservations(input.id).some(row => row.payload.kind === "compute"))
      throw Error("A new native research batch requires a separately registered identity-exclusion capability")
    const plans = researchNativePlans(input.research, input.id)
    if (plans.some(item => item.offer.prepared.plan.id === prepared.plan.id)) throw Error("Native plan identity already has another host key")
    input.research.record(input.id, offerRef(input.key), offer)
    return { evidence_ref: offerRef(input.key), plan_id: prepared.plan.id, registered: true }
  }).immediate()
}

/** Host-only durable registry. No model-authored path, command or arguments can
 * create an entry. A used key can never become an executable offer again. */
export function researchNativePlans(research: ResearchLoopStore, id: string) {
  const loop = research.get(id)
  const reservations = research.reservations(id)
  return research.evidence(id, "host").filter(row => row.key.startsWith("native-plan:")).map(row => {
    const offer = registeredPlan.parse(row.payload)
    if (row.key !== offerRef(offer.key) || offer.loop_id !== id || offer.prepared.contract_digest !== loop.digest ||
      researchDigest(offer.prepared.contract) !== loop.digest)
      throw Error("Native plan offer lost its immutable loop or contract binding")
    return { offer, evidence_ref: row.key, available: loop.contract.mode === "native-compute"
      && ["READY", "DECIDING"].includes(loop.state) && !reservations.some(item => item.payload.kind === "compute") }
  })
}

/** Called inside the controller's decision transaction, after its guard. */
export function selectResearchNativePlan(research: ResearchLoopStore, id: string, decisionID: string, decision: ResearchDecision) {
  const item = researchNativePlans(research, id).find(item => item.available && item.offer.prepared.plan.id === decision.plan_id)
  if (decision.action !== "execute_prepared_action" || decision.capability_id !== nativeResearchCapability ||
    !item || !decision.evidence_refs.includes(item.evidence_ref)) throw Error("Execution does not select an available host-native plan")
  const plan = item.offer.prepared
  if (Date.now() >= plan.plan.approve_before) throw Error("Selected native plan expired")
  research.reserve(id, research.get(id).revision, { key: item.offer.key, kind: "compute", round: item.offer.round,
    candidate_slots: plan.plan.shortlist, plan_digest: plan.plan_digest, source_release: plan.contract.inputs.source_release,
    resources: plan.resources })
  const selected = selection.parse({ schema: "drug-design-native-plan-selection-v1", decision_id: decisionID,
    offer_ref: item.evidence_ref, key: item.offer.key, task_id: plan.plan.id, plan_digest: plan.plan_digest })
  research.record(id, selectionRef(decisionID), selected)
  return selected
}

function pending(input: Identity) {
  const loop = input.research.get(input.id)
  const decisions = input.research.evidence(input.id, "decision")
  const evidence = input.research.evidence(input.id, "host")
  // A later inspection/finish decision must not replace the durable identity of
  // the native action whose result can still arrive or be reread.
  const last = input.decision_id ? decisions.find(row => row.key === input.decision_id)
    : decisions.findLast(row => evidence.some(item => item.key === selectionRef(row.key)))
  if (!last) throw Error("No model-selected native action exists")
  const payload = last.payload as { decision?: unknown; provenance?: { kind?: string } }
  const decision = ResearchDecision.parse(payload.decision)
  const guard = evidence.find(row => row.key === `guard-${last.key}`)?.payload as { accepted?: boolean } | undefined
  const selected = selection.parse(evidence.find(row => row.key === selectionRef(last.key))?.payload)
  const item = researchNativePlans(input.research, input.id).find(item => item.evidence_ref === selected.offer_ref)
  if (payload.provenance?.kind !== "model_response" || !guard?.accepted || decision.action !== "execute_prepared_action" ||
    decision.capability_id !== nativeResearchCapability || selected.decision_id !== last.key || !item ||
    decision.plan_id !== selected.task_id || selected.task_id !== item.offer.prepared.plan.id ||
    selected.plan_digest !== item.offer.prepared.plan_digest || selected.key !== item.offer.key)
    throw Error("Pending native action does not match its guarded model decision and frozen plan")
  const reservation = input.research.reservations(input.id).find(row => row.key === selected.key)
  if (!reservation || reservation.payload.kind !== "compute" || reservation.payload.plan_digest !== selected.plan_digest ||
    reservation.payload.source_release !== loop.contract.inputs.source_release ||
    (reservation.task !== null && reservation.task !== selected.task_id) ||
    researchDigest(reservation.payload.resources) !== researchDigest(item.offer.prepared.resources))
    throw Error("Selected native action has no matching cumulative reservation")
  return { loop, decision_id: last.key, selected, prepared: item.offer.prepared,
    reference: { loop: input.id, key: selected.key, contract_digest: loop.digest }, reservation }
}

function cancellation(input: Identity, value: ReturnType<typeof pending>, execution: ReturnType<typeof readResearchExecution>) {
  if (!execution || execution.task.state !== "CANCELLED") return { kind: null, proof: null }
  const submitted = execution.stages.some(stage => stage.submitted !== null || stage.handle !== null || stage.manifest !== null || stage.state !== "WAITING")
  const fallback = { kind: submitted ? "cancelled_after_submission" as const : "cancelled_before_submission" as const, proof: null }
  const raw = input.research.evidence(input.id, "host").find(row => row.key === rejectionRef(value.decision_id))
  if (!raw || execution.task.approval || submitted) return fallback
  const reply = rejection.parse(raw.payload)
  if (reply.loop_id !== input.id || reply.decision_id !== value.decision_id || reply.task_id !== execution.task.id
    || reply.plan_digest !== execution.task.digest || reply.session_id !== value.loop.contract.session
    || reply.request_patterns[0] !== execution.task.digest) throw Error("Native rejection receipt lost its exact permission/task binding")
  const events = z.array(z.object({ sequence: z.number().int(), kind: z.string(), payload: z.string() }).passthrough()).parse(execution.events)
  // Cancellation alone is not a user rejection. Require the real permission
  // path, cleanup journal, untouched stages and absence of any dispatch/grant.
  if (events.some(event => ["approved", "dispatch_reserved", "stage_state"].includes(event.kind))) return fallback
  const requested = events.findLast(event => event.kind === "approval_requested" && (() => {
    const item = JSON.parse(event.payload) as { digest?: string; session?: string }
    return item.digest === execution.task.digest && item.session === value.loop.contract.session
  })())
  const ended = events.findLast(event => event.kind === "approval_ended_without_grant" && (() => {
    const item = JSON.parse(event.payload) as { digest?: string }
    return item.digest === execution.task.digest
  })())
  if (!requested || !ended || ended.sequence <= requested.sequence) return fallback
  const proof = declined.parse({ schema: "drug-design-native-approval-declined-v1", loop_id: input.id,
    decision_id: value.decision_id, task_id: execution.task.id, plan_digest: execution.task.digest,
    reservation_key: value.selected.key, rejection_ref: raw.key, native_state: "CANCELLED", approval: null,
    all_stages_unsubmitted: true, approval_requested_sequence: requested.sequence, approval_ended_sequence: ended.sequence,
    actual_usage: null, accounting_complete: false, reservation_released: false })
  return { kind: "approval_declined" as const, proof }
}

/** Only confirmations produced from the native journal and a typed permission
 * rejection count. They permit ending a refused action, never zero settlement. */
export function researchNativeApprovalDeclined(research: ResearchLoopStore, id: string) {
  const loop = research.get(id), evidence = research.evidence(id, "host"), decisions = research.evidence(id, "decision")
  const reservations = research.reservations(id).filter(row => row.payload.kind === "compute")
  const proofs = evidence.filter(row => row.key.startsWith("native-approval-declined:")).map(row => {
    const proof = declined.parse(row.payload)
    const reply = rejection.parse(evidence.find(item => item.key === proof.rejection_ref)?.payload)
    const selected = selection.parse(evidence.find(item => item.key === selectionRef(proof.decision_id))?.payload)
    const record = decisions.find(item => item.key === proof.decision_id)?.payload as { decision?: unknown; provenance?: { kind?: string } } | undefined
    const decision = ResearchDecision.parse(record?.decision)
    const reserved = reservations.find(item => item.key === proof.reservation_key)
    if (row.key !== declinedRef(proof.decision_id) || proof.loop_id !== id || reply.loop_id !== id
      || proof.rejection_ref !== rejectionRef(proof.decision_id) || reply.decision_id !== proof.decision_id
      || reply.session_id !== loop.contract.session || reply.task_id !== proof.task_id || reply.plan_digest !== proof.plan_digest
      || reply.request_patterns[0] !== proof.plan_digest || selected.task_id !== proof.task_id || selected.plan_digest !== proof.plan_digest
      || selected.key !== proof.reservation_key || selected.decision_id !== proof.decision_id
      || record?.provenance?.kind !== "model_response" || decision.action !== "execute_prepared_action"
      || decision.capability_id !== nativeResearchCapability || decision.plan_id !== proof.task_id
      || reserved?.task !== proof.task_id || reserved.payload.plan_digest !== proof.plan_digest
      || proof.approval_ended_sequence <= proof.approval_requested_sequence) throw Error("Native approval refusal proof is inconsistent")
    return { evidence_ref: row.key, key: reserved.key, state: reserved.state }
  })
  return { declined: proofs.length > 0, evidence_refs: proofs.map(item => item.evidence_ref),
    unsubmitted_reservations: new Set(proofs.filter(item => item.state === "RESERVED").map(item => item.key)).size }
}

/** Recovery reads the original task; it never opens an approval or starts a
 * daemon. Missing task after a crash is reported, never replaced by a new ID. */
export function readResearchNativeAction(input: Identity) {
  const value = pending(input)
  const execution = readResearchExecution(input.native, value.reference)
  if (execution && execution.task.id !== value.selected.task_id) throw Error("Native recovery found a different task")
  return { ...value, execution, task_id: value.selected.task_id,
    status: !execution ? "not_prepared" as const : execution.task.state,
    cancellation: cancellation(input, value, execution).kind,
    compute_usage: value.reservation.actual, observation_required: Boolean(execution && ["SUCCEEDED", "FAILED", "CANCELLED"].includes(execution.task.state)) }
}

/** Explicit host dispatch only. The existing native bridge owns exact-plan
 * permission, once claims and scheduling; this module cannot approve on behalf
 * of the model and never performs raw submission. */
export async function advanceResearchNativeAction(input: Identity & { context: NativeContext; phase: "approve" | "start" }) {
  input.context.abort.throwIfAborted()
  const value = pending(input)
  if (!["PREPARING", "AWAITING_APPROVAL", "RUNNING"].includes(value.loop.state) || value.reservation.state !== "RESERVED")
    throw Error("Research state does not permit native dispatch")
  if (input.context.sessionID !== value.loop.contract.session || input.context.directory !== value.loop.contract.directory)
    throw Error("Native action host context belongs to another conversation")
  const bridge = { research: input.research, native: input.native, reference: value.reference, context: input.context }
  if (input.phase === "approve") {
    const claimed = input.research.events(input.id).some(event => event.kind === "submission_claimed"
      && (event.payload as { key?: string }).key === value.selected.key)
    if (claimed && !readResearchExecution(input.native, value.reference))
      throw Error("Claimed native task is missing; reconcile the original task instead of recreating it")
    await prepareResearchExecution(input.native, value.prepared, value.reference)
    input.research.bindTask(input.id, value.selected.key, value.selected.task_id, value.selected.plan_digest)
    const { PermissionNext } = await import("../../src/permission/next")
    const result = await approveResearchNative({ ...bridge, context: { ...input.context, ask: async request => {
      try { await input.context.ask(request) }
      catch (error) {
        if (!input.context.abort.aborted && (error instanceof PermissionNext.RejectedError || error instanceof PermissionNext.CorrectedError)
          && request.permission === "remote_compute" && request.patterns.length === 1 && request.patterns[0] === value.selected.plan_digest) {
          input.research.record(input.id, rejectionRef(value.decision_id), rejection.parse({ schema: "drug-design-native-permission-rejection-v1",
            loop_id: input.id, decision_id: value.decision_id, task_id: value.selected.task_id, plan_digest: value.selected.plan_digest,
            session_id: value.loop.contract.session, permission: "remote_compute",
            kind: error instanceof PermissionNext.CorrectedError ? "corrected" : "rejected", request_patterns: request.patterns }))
          // Never persist the user's optional private correction text in the
          // downstream bridge's generic error receipt.
          throw new PermissionNext.RejectedError()
        }
        throw error
      }
    } } })
    const disposition = cancellation(input, pending(input), readResearchExecution(input.native, value.reference))
    if (disposition.proof) input.research.record(input.id, declinedRef(value.decision_id), disposition.proof)
    input.research.record(input.id, `native-action-approval:${value.decision_id}:${researchDigest(result)}`, {
      schema: "drug-design-native-action-approval-v1", decision_id: value.decision_id, result,
    })
    return { decision_id: value.decision_id, phase: input.phase, result, cancellation: disposition.kind,
      reservation_released: false, accounting_complete: false, loop: input.research.get(input.id) }
  }
  const result = await startResearchNative(bridge)
  input.research.record(input.id, `native-action-start:${value.decision_id}:${researchDigest(result)}`, {
    schema: "drug-design-native-action-start-v1", decision_id: value.decision_id, result,
  })
  const current = input.research.get(input.id)
  if (result.state === "observer_ready" && !["FINISHED", "CANCELLED", "BLOCKED"].includes(current.state))
    input.research.transition(input.id, current.revision, "RUNNING", { reason: "native_supervisor_attached",
      task_id: value.selected.task_id, task_running_claimed: false })
  return { decision_id: value.decision_id, phase: input.phase, result, loop: input.research.get(input.id) }
}

/** The Results/evaluator adapter consumes this hash-bound receipt. It contains
 * fresh local readback and native task lineage, but never creates scientific
 * observations or invents billing from allocations/timings. */
export async function captureResearchNativeActionResult(input: Identity) {
  const value = pending(input)
  const receipt = await readResearchNativeReceipt({ research: input.research, native: input.native, reference: value.reference })
  const disposition = cancellation(input, value, readResearchExecution(input.native, value.reference))
  if (disposition.proof) input.research.record(input.id, declinedRef(value.decision_id), disposition.proof)
  const { observed_at, ...content } = receipt
  const evidence_ref = `native-result:${value.decision_id}:${researchDigest(content)}`
  input.research.record(input.id, evidence_ref, { schema: "drug-design-native-action-result-v1",
    decision_id: value.decision_id, receipt: content })
  return { evidence_ref, receipt, observed_at, decision_id: value.decision_id, reference: value.reference,
    cancellation: disposition.kind, scientific_evaluation_performed: false, ready_for_evaluation: ["SUCCEEDED", "FAILED", "CANCELLED"].includes(receipt.task_state),
    accounting_status: input.research.reservations(input.id)
      .find(row => row.key === value.selected.key)!.state, loop: input.research.get(input.id) }
}
