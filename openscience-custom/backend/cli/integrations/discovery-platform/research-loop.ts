import z from "zod"
import { guardResearchAction } from "./research-action-guard"
import { buildResearchDecisionPrompt, ResearchDecision, type ResearchDecisionContext } from "./research-decision"
import { captureResearchArtifact, verifyResearchObservation } from "./research-observations"
import { ResearchLoopStore } from "./research-loop-store"
import { nativeResearchCapability, researchNativePlans, selectResearchNativePlan, researchNativeApprovalDeclined } from "./research-native-controller"

/** Host capabilities and already verified native plans are explicit. The model
 * may select a plan; only a separate native host context can request approval. */
export function researchSnapshot(store: ResearchLoopStore, id: string) {
  return store.db.transaction(() => {
  const loop = store.get(id)
  const events = store.events(id)
  const evidence = store.evidence(id)
  const observations = evidence.filter(item => item.kind === "observation").map(item => verifyResearchObservation(item.payload))
  const terminal = loop.state === "FINISHED" || loop.state === "CANCELLED"
  const inspect = observations.filter(item => !evidence.some(row => row.key === `inspection-${item.id}`))
  const nativePlans = researchNativePlans(store, id).filter(item => item.available)
  const capabilities: ResearchDecisionContext["capabilities"] = [...(inspect.length ? [{
    id: "research.inspect-recorded-artifacts.v1", actions: ["inspect_evidence"], ready: !terminal,
    schema: z.strictObject({ observation_id: z.enum(inspect.map(item => item.id) as [string, ...string[]]) }),
    description: "Re-read and hash the local files already bound to an observation; cannot select arbitrary files or run chemistry.",
    version: "1", limitations: ["No scientific evaluator or remote submission is implied by inspecting artifacts"],
  } satisfies ResearchDecisionContext["capabilities"][number]] : []), ...(nativePlans.length ? [{
    id: nativeResearchCapability, actions: ["execute_prepared_action"], ready: !terminal,
    schema: z.strictObject({}), version: "1",
    description: "Select one exact host-verified native plan. The host separately requests native permission and schedules only that plan.",
    limitations: ["No model approval, arbitrary commands, replacement inputs, scientific success or billing claim"],
  } satisfies ResearchDecisionContext["capabilities"][number]] : [])]
  const context: ResearchDecisionContext = {
    loop_id: id, state_revision: loop.revision, evidence_refs: evidence.map(item => item.key), capabilities,
    allowed_actions: terminal ? [] : [
      { action: "finish" }, { action: "blocked" }, { action: "request_user_input" },
      ...(inspect.length ? [{ action: "inspect_evidence" as const, capability_id: "research.inspect-recorded-artifacts.v1" }] : []),
      ...nativePlans.map(item => ({ action: "execute_prepared_action" as const, capability_id: nativeResearchCapability,
        plan_id: item.offer.prepared.plan.id })),
    ],
    prepared_plans: nativePlans.map(item => ({ id: item.offer.prepared.plan.id, capability_id: nativeResearchCapability,
      evidence_ref: item.evidence_ref })),
  }
  const payload = { research_contract: loop.contract,
    research_state: { id, revision: loop.revision, state: loop.state, outcome: loop.outcome,
      host_evidence: evidence.filter(item => item.kind === "host").map(item => ({ ref: item.key, sha256: item.digest,
        // Model receipts retain their full prompt and raw response in the store.
        // Feeding those recursively into the next prompt causes unbounded growth.
        content: compactHostEvidence(item.payload) })),
      decision_history: evidence.filter(item => item.kind === "decision").map(item => ({ ref: item.key, content: item.payload })),
      limitations: ["next_frozen_batch is unavailable: current selector cannot exclude previously evaluated identities",
        "Native execution requires a registered exact plan and a separate host approval/start action",
        "Native Results require independent scientific evaluation and verified resource settlement before objective completion"] },
    resource_ledger: loop.ledger, observations }
  return { loop, context, payload, observations, events, prompt: buildResearchDecisionPrompt(context, payload) }
  }).deferred()
}

function compactHostEvidence(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value
  const record = value as Record<string, unknown>
  if (record.schema === "drug-design-native-plan-offer-v1") {
    const prepared = record.prepared as { plan: { id: string; target: string; site: string; max_records: number; shortlist: number; finish_before: number };
      plan_digest: string; contract_digest: string; resources: unknown }
    return { schema: record.schema, loop_id: record.loop_id, key: record.key, round: record.round,
      plan_id: prepared.plan.id, plan_digest: prepared.plan_digest, contract_digest: prepared.contract_digest,
      target: prepared.plan.target, site: prepared.plan.site, max_records: prepared.plan.max_records, shortlist: prepared.plan.shortlist,
      finish_before: prepared.plan.finish_before, reserved_if_selected: prepared.resources }
  }
  if (typeof record.schema !== "string" || !record.schema.includes("model")) return value
  return Object.fromEntries(Object.entries(record).filter(([key]) =>
    !["request", "prepared_context", "prepared_payload", "prepared_prompt", "raw_response", "response", "result", "prompt", "system", "text"].includes(key)))
}

/** One local replay transition. This tests the real host guards and persistence,
 * but is not an LLM acceptance test. The live model/approval driver is a separate
 * integration milestone; this API cannot control a native computation. */
export async function applyResearchReplayProposal(input: {
  store: ResearchLoopStore; id: string; value: unknown; case_id: string; now?: number
}) {
  const store = input.store
  const snapshot = researchSnapshot(store, input.id)
  if (snapshot.loop.contract.mode !== "offline-replay")
    throw Error("Scripted replay cannot control a native research loop")
  if (!input.case_id.trim()) throw Error("Replay requires an explicit case identity")
  const limit = store.evidence(input.id, "decision").length >= snapshot.loop.contract.limits.max_decisions
    || (input.now ?? Date.now()) >= snapshot.loop.created + snapshot.loop.contract.limits.max_elapsed_ms
  if (limit && !["FINISHED", "CANCELLED"].includes(snapshot.loop.state)) {
    // A host stop must remain possible after the last decision slot is consumed.
    // It is not an extra model/script decision and never resets the budget.
    const key = "host-budget-stop"
    store.record(input.id, key, { schema: "drug-design-budget-stop-v1", based_on_revision: snapshot.loop.revision,
      decisions: store.evidence(input.id, "decision").length, now: input.now ?? Date.now(), ledger: snapshot.loop.ledger }, input.now)
    const loop = store.finish(input.id, store.get(input.id).revision, "budget_exhausted", [key], input.now)
    return { decision: null, accepted: false, reasons: ["Host stopped at the cumulative decision or wall-clock limit"],
      acceptance_scope: snapshot.loop.contract.mode, new_research_result: false,
      evaluated_observation_ids: [] as string[], loop, decision_id: null }
  }
  return commitResearchDecision({ store, id: input.id, value: input.value, context: snapshot.context,
    expected_revision: snapshot.loop.revision,
    provenance: { kind: "scripted_replay", case_id: input.case_id }, now: input.now })
}

/** Host-only commit boundary. A live caller passes the exact context returned
 * by the budgeted model call and its CAS revision after receipt settlement.
 * No rebasing of model-supplied revisions, arguments or evidence is performed. */
export async function commitResearchDecision(input: {
  store: ResearchLoopStore; id: string; value: unknown; context: ResearchDecisionContext;
  expected_revision: number; provenance: { kind: "scripted_replay"; case_id: string }
    | { kind: "model_response"; receipt_ref: string; request_digest: string };
  now?: number
}) {
  const store = input.store
  const committed = store.db.transaction(() => {
  const snapshot = researchSnapshot(store, input.id)
  if (snapshot.loop.revision !== input.expected_revision) throw Error("Stale research state before decision commit")
  if (!["READY", "DECIDING"].includes(snapshot.loop.state)) throw Error("Research loop is not ready for another decision")
  if (input.provenance.kind === "scripted_replay" && snapshot.loop.contract.mode !== "offline-replay")
    throw Error("Scripted replay cannot control a native research loop")
  if (input.provenance.kind === "model_response") {
    const provenance = input.provenance
    const receipt = store.evidence(input.id, "host").find(item => item.key === provenance.receipt_ref)
    if (!receipt) throw Error("Live decision requires its immutable model receipt")
    if (store.evidence(input.id, "decision").some(item => {
      const previous = item.payload as { provenance?: { receipt_ref?: string } }
      return previous.provenance?.receipt_ref === provenance.receipt_ref
    })) throw Error("Model receipt already has a committed decision")
  }
  const reservations = store.reservations(input.id)
  const refusal = researchNativeApprovalDeclined(store, input.id)
  const result = guardResearchAction(input.value, { contract: snapshot.loop.contract, context: input.context,
    observations: snapshot.observations, resources: snapshot.loop.ledger.total,
    unsettled_compute: reservations.filter(item => item.payload.kind !== "model" && item.state !== "SETTLED").length,
    completed_rounds: reservations.filter(item => item.payload.kind === "compute").length,
    decisions_used: store.evidence(input.id, "decision").length,
    deadline: snapshot.loop.created + snapshot.loop.contract.limits.max_elapsed_ms,
    now: input.now ?? Date.now(), bound_task_ids: reservations.flatMap(item => item.task ? [item.task] : []),
    user_cancelled: false, approval_declined: refusal.declined,
    approval_declined_unsubmitted: refusal.unsubmitted_reservations, approval_declined_refs: refusal.evidence_refs,
  })
  if (!["finish", "blocked", "request_user_input", "inspect_evidence", "execute_prepared_action"].includes(result.decision.action))
    throw Error("This action has no attached executor")
  const saved = store.decide(input.id, input.expected_revision, {
    schema: "drug-design-recorded-proposal-v1", decision: result.decision,
    provenance: input.provenance,
  }, input.now)
  const key = `guard-${saved.decision_id}`
  store.record(input.id, key, { schema: "drug-design-action-guard-v1", ...result }, input.now)
  const current = () => store.get(input.id)
  if (!result.accepted) {
    store.transition(input.id, current().revision, "BLOCKED", { reason: "action_guard_rejected", evidence: key }, input.now)
    store.record(input.id, `action-completed:${saved.decision_id}`, { action: "guard_rejected", evidence: key }, input.now)
    return { result, decision_id: saved.decision_id, pending: false }
  }
  if (result.decision.action === "execute_prepared_action") {
    if (input.provenance.kind !== "model_response") throw Error("Native execution requires a real model-response commit boundary")
    const selected = selectResearchNativePlan(store, input.id, saved.decision_id, result.decision)
    store.transition(input.id, current().revision, "PREPARING", { reason: "host_native_plan_selected",
      decision_id: saved.decision_id, task_id: selected.task_id, reservation_key: selected.key }, input.now)
    return { result, decision_id: saved.decision_id, pending: "native" as const }
  }
  if (result.decision.action === "finish") {
    store.finish(input.id, current().revision, result.decision.outcome!, result.decision.evidence_refs, input.now)
  }
  if (result.decision.action === "blocked" || result.decision.action === "request_user_input") {
    store.transition(input.id, current().revision, "BLOCKED", {
      reason: result.decision.outcome ?? "missing_user_input", missing: result.decision.missing_inputs, evidence: key,
    }, input.now)
  }
  if (result.decision.action === "inspect_evidence") {
    store.transition(input.id, current().revision, "EVALUATING", {
      reason: "inspecting_recorded_artifacts", decision_id: saved.decision_id, observation_id: result.decision.arguments.observation_id,
    }, input.now)
    return { result, decision_id: saved.decision_id, pending: "local" as const }
  }
  store.record(input.id, `action-completed:${saved.decision_id}`, { action: result.decision.action, evidence: key }, input.now)
  return { result, decision_id: saved.decision_id, pending: false }
  }).immediate()
  if (committed.pending === "local") await recoverLocalResearchAction(store, input.id, input.now)
  return { ...committed.result, loop: store.get(input.id), decision_id: committed.decision_id,
    pending_native_action: committed.pending === "native" }
}

/** Resume an already committed read-only action without calling the model again.
 * Guard+decision+pending state are committed in one SQLite transaction. */
export async function recoverLocalResearchAction(store: ResearchLoopStore, id: string, now?: number) {
    const snapshot = researchSnapshot(store, id)
    if (snapshot.loop.state !== "EVALUATING") return { resumed: false, loop: snapshot.loop }
    const rows = store.evidence(id, "decision")
    const last = rows.at(-1)
    if (!last) throw Error("Pending research action has no decision evidence")
    if (store.evidence(id, "host").some(item => item.key === `action-completed:${last.key}`))
      throw Error("Completed research action cannot remain pending")
    const recorded = last.payload as { decision?: unknown }
    const decision = ResearchDecision.parse(recorded.decision)
    const guard = store.evidence(id, "host").find(item => item.key === `guard-${last.key}`)?.payload as { accepted?: boolean } | undefined
    if (!guard?.accepted || decision.action !== "inspect_evidence") throw Error("Pending local action is not a guarded inspection")
    const observation = snapshot.observations.find(item => item.id === decision.arguments.observation_id)
    if (!observation) throw Error("Pending inspection lost its observation")
    const artifacts = await Promise.all(observation.artifacts.map(item => captureResearchArtifact({
      path: item.path, ref: item.ref, expectedSha256: item.sha256 ?? undefined,
    })))
    const failed = artifacts.length === 0 || artifacts.some(item => item.status !== "verified")
    return store.db.transaction(() => {
    const current = store.get(id)
    const completed = store.evidence(id, "host").some(item => item.key === `action-completed:${last.key}`)
    if (completed) return { resumed: false, loop: current }
    // Cancellation or a new event cannot be overwritten by an in-flight read.
    if (current.revision !== snapshot.loop.revision) throw Error("Research state changed during artifact inspection")
    store.record(id, `inspection-${observation.id}`, {
      schema: "drug-design-artifact-inspection-v1", observation_id: observation.id,
      artifacts, passed: !failed, scientific_evaluation_performed: false,
    }, now)
    store.transition(id, store.get(id).revision, failed ? "BLOCKED" : "DECIDING", {
      reason: failed ? "recorded_artifacts_changed_missing_or_unanchored" : "recorded_artifacts_verified",
      evidence: `inspection-${observation.id}`,
    }, now)
    store.record(id, `action-completed:${last.key}`, { action: decision.action, evidence: `inspection-${observation.id}` }, now)
    return { resumed: true, loop: store.get(id) }
    }).immediate()
}
