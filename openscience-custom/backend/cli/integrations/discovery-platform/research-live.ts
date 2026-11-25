import z from "zod"
import { buildResearchDecisionPrompt, parseResearchDecision, ResearchAction, type ResearchDecisionContext } from "./research-decision"
import { emptyResources, researchDigest, resourceKeys, exceedsResearchLimit } from "./research-loop-contract"
import { ResearchLoopStore } from "./research-loop-store"
import { commitResearchDecision, recoverLocalResearchAction, researchSnapshot } from "./research-loop"
import { researchModelCost, normalizeResearchModelUsage, readResearchModelCall, runBudgetedResearchDecision,
  type ResearchModelBudgetPolicy, type ResearchModelRequest } from "./research-model-budget"

type Identity = { store: ResearchLoopStore; id: string; key: string }
type Call = NonNullable<ReturnType<typeof readResearchModelCall>>
export type ResearchLiveInput = Identity & {
  request: Omit<ResearchModelRequest, "context" | "payload">
  policy: ResearchModelBudgetPolicy
}

const promptSchema = z.strictObject({
  mode: z.literal("decision"), research_contract: z.unknown(), research_state: z.unknown(), resource_ledger: z.unknown(),
  observations: z.array(z.unknown()),
  runtime_identity: z.strictObject({ loop_id: z.string().min(1), state_revision: z.number().int().nonnegative() }),
  evidence_refs: z.array(z.string().min(1)),
  allowed_actions: z.array(z.strictObject({ action: ResearchAction, capability_id: z.string().nullable().optional(),
    plan_id: z.string().nullable().optional() })),
  prepared_plans: z.array(z.strictObject({ id: z.string(), capability_id: z.string(), evidence_ref: z.string() })),
  capability_registry: z.array(z.object({ id: z.string(), version: z.string().optional(), arguments_schema: z.unknown() }).passthrough()),
})

/** Rehydrate only trusted, still-attached host validators. A stored JSON schema
 * cannot create a capability, and changed code/arguments require a new review.
 * Original prompt order and bytes are checked; no revision is rebased. */
function restore(call: Call, snapshot: ReturnType<typeof researchSnapshot>) {
  const value = promptSchema.parse(JSON.parse(call.attempt.prepared_prompt.user))
  const capabilities = value.capability_registry.map(item => {
    const capability = snapshot.context.capabilities.find(candidate => candidate.id === item.id && candidate.version === item.version)
    if (!capability) throw Error("Prepared model capability is no longer attached at its frozen version")
    const { schema, ...metadata } = capability
    if (researchDigest({ ...metadata, arguments_schema: z.toJSONSchema(schema, { io: "input" }) }) !== researchDigest(item))
      throw Error("Prepared model capability differs from its current host validator")
    return capability
  })
  const context: ResearchDecisionContext = {
    ...value.runtime_identity, evidence_refs: value.evidence_refs, allowed_actions: value.allowed_actions,
    prepared_plans: value.prepared_plans, capabilities,
  }
  const payload = { research_contract: value.research_contract, research_state: value.research_state,
    resource_ledger: value.resource_ledger, observations: value.observations }
  const prompt = buildResearchDecisionPrompt(context, payload)
  if (prompt.sha256 !== call.attempt.prepared_prompt.sha256 || prompt.system !== call.attempt.prepared_prompt.system
    || prompt.user !== call.attempt.prepared_prompt.user)
    throw Error("Prepared model context cannot be recovered byte-for-byte")
  if (context.loop_id !== snapshot.loop.id || context.state_revision !== call.attempt.prepared_revision
    || researchDigest(payload.research_contract) !== researchDigest(snapshot.loop.contract))
    throw Error("Prepared model context does not match its frozen loop and contract")
  return { context, payload }
}

function consumed(store: ResearchLoopStore, id: string, reference: string) {
  return store.evidence(id, "decision").find(row => {
    const payload = row.payload as { provenance?: { kind?: string; receipt_ref?: string } }
    return payload.provenance?.kind === "model_response" && payload.provenance.receipt_ref === reference
  })
}

/** Validate actual provider text, provenance and settled resource receipts, not
 * the model's own claim that it is valid or that the task has completed. */
function applicable(input: Identity, call: Call, snapshot: ReturnType<typeof researchSnapshot>) {
  const receipt = call.receipt
  const result = receipt?.result
  if (!receipt || !result || receipt.status !== "completed" || receipt.can_apply !== true || call.can_apply !== true
    || receipt.decision_validated !== true || receipt.state_changed_during_call !== false
    || receipt.failure !== null || !Array.isArray(receipt.bounds_violated) || receipt.bounds_violated.length
    || !["settled", "usage-settled-cost-unknown"].includes(receipt.accounting?.state) || receipt.accounting.reason !== null || result.validated !== true)
    throw Error("Model receipt is not a complete, unchanged, settled decision")
  if (receipt.base_revision !== call.attempt.base_revision || receipt.prepared_revision !== call.attempt.prepared_revision
    || call.attempt.prepared_revision !== call.attempt.base_revision + 2
    || receipt.expected_revision !== receipt.prepared_revision + 2 || receipt.expected_revision !== snapshot.loop.revision)
    throw Error("Model receipt revision chain is inconsistent")
  if (result.finish_reason !== "stop" || result.prompt_hash_scope !== "domain-and-decision-context"
    || result.prompt_sha256 !== call.attempt.prepared_prompt.sha256
    || result.model.providerID !== call.attempt.policy.provider_id || result.model.modelID !== call.attempt.policy.model_id
    || !call.attempt.policy.response_model_ids.includes(result.model.response_model_id))
    throw Error("Model result source or prompt binding is invalid")
  const normalized = normalizeResearchModelUsage(result.usage)
  if (!normalized.complete || researchDigest(normalized.usage) !== researchDigest(receipt.accounting.usage)
    || normalized.usage.input_tokens > call.attempt.policy.input_token_cap
    || normalized.usage.output_tokens > call.attempt.max_output_tokens)
    throw Error("Model result has unknown, inconsistent or excessive usage")
  const price = call.attempt.policy.pricing
  const charge = researchModelCost(normalized.usage.input_tokens, normalized.usage.output_tokens, call.attempt.policy)
  if (receipt.accounting.model_cost_upper_bound !== charge
    || receipt.accounting.currency !== price.currency || receipt.accounting.pricing_basis !== price.basis
    || receipt.accounting.state !== (charge === null ? "usage-settled-cost-unknown" : "settled")
    || (charge === null && snapshot.loop.contract.limits.resources.model_cost !== null))
    throw Error("Model receipt cost ceiling does not match its frozen price policy")
  const reservation = input.store.reservations(input.id).find(row => row.key === input.key)
  if (reservation?.state !== "SETTLED" || reservation.receipt !== call.receipt_ref
    || researchDigest(reservation.actual) !== researchDigest({ ...emptyResources(), model_tokens: normalized.usage.total_tokens, model_cost: charge ?? 0 }))
    throw Error("Model receipt has no exact matching resource settlement")
  if (resourceKeys.some(key => exceedsResearchLimit(snapshot.loop.ledger.total[key], snapshot.loop.contract.limits.resources[key]))
    || input.store.reservations(input.id).some(row => row.actual
      && resourceKeys.some(key => row.actual![key] > row.payload.resources[key] + 1e-9)))
    throw Error("Cumulative or per-call resource bounds were exceeded")
  const prepared = restore(call, snapshot)
  const decision = parseResearchDecision(result.text, prepared.context)
  if (researchDigest(decision) !== researchDigest(result.decision)) throw Error("Validated model decision differs from the actual response text")
  return { ...prepared, decision, expected_revision: receipt.expected_revision }
}

/** Resume a durable receipt or a committed local action. This entry point never
 * resolves a provider, invokes a model, approves compute, or submits a task. */
export async function recoverResearchLiveDecision(input: Identity) {
  if (!input.key.trim()) throw Error("Research decision requires an idempotency key")
  if (input.store.get(input.id).state === "EVALUATING") {
    const action = await recoverLocalResearchAction(input.store, input.id)
    return { status: "action_recovered" as const, invoked: false, ...action }
  }
  const state = input.store.db.transaction(() => {
    const call = readResearchModelCall(input.store, input.id, input.key)
    const loop = input.store.get(input.id)
    if (!call) return { status: "no_attempt" as const, loop, call }
    const previous = consumed(input.store, input.id, `model-receipt:${input.key}`)
    if (previous) return { status: "already_committed" as const, loop, call, decision_id: previous.key }
    if (!call.receipt) return { status: "in_flight_or_usage_unknown" as const, loop, call }
    if (!call.can_apply || !["READY", "DECIDING"].includes(loop.state))
      return { status: "receipt_not_applicable" as const, loop, call }
    const prepared = applicable(input, call, researchSnapshot(input.store, input.id))
    return { status: "ready" as const, loop, call, prepared }
  }).deferred()
  if (state.status !== "ready") return { ...state, invoked: false }
  const commit = await commitResearchDecision({ store: input.store, id: input.id, value: state.prepared.decision,
    context: state.prepared.context, expected_revision: state.prepared.expected_revision,
    provenance: { kind: "model_response", receipt_ref: state.call.receipt_ref!, request_digest: state.call.attempt.request_digest } })
  return { status: "decision_committed" as const, invoked: false, call: state.call, commit, loop: commit.loop }
}

/** One host-owned live decision. Recovery is always attempted before provider
 * configuration is read. Only the budget driver's first durable claimant can
 * invoke the platform LLM, and the settled receipt is then independently checked.
 * Callers must preserve the key; changing it cannot bypass an older pending call. */
export async function runResearchLiveDecision(input: ResearchLiveInput) {
  const recovered = await recoverResearchLiveDecision(input)
  if (recovered.status !== "no_attempt") return recovered
  const outstanding = input.store.reservations(input.id).find(row => row.payload.kind === "model"
    && !consumed(input.store, input.id, `model-receipt:${row.key}`))
  if (outstanding) {
    const prior = await recoverResearchLiveDecision({ ...input, key: outstanding.key })
    if (prior.status === "no_attempt") throw Error("Outstanding model reservation has no durable invocation provenance; reconcile it before proceeding")
    return prior
  }
  input.request.abort.throwIfAborted()
  const snapshot = researchSnapshot(input.store, input.id)
  if (!["READY", "DECIDING"].includes(snapshot.loop.state)) throw Error("Research loop is not ready for a model decision")
  const now = Date.now()
  const decisions = input.store.evidence(input.id, "decision").length
  const calls = input.store.reservations(input.id).filter(row => row.payload.kind === "model").length
  if (Math.max(decisions, calls) >= snapshot.loop.contract.limits.max_decisions
    || now >= snapshot.loop.created + snapshot.loop.contract.limits.max_elapsed_ms) {
    // A host stop is possible after the final decision slot and cannot reset
    // the original wall-clock budget or release uncertain reservations.
    const loop = input.store.db.transaction(() => {
      if (input.store.get(input.id).revision !== snapshot.loop.revision) throw Error("Research state changed before host budget stop")
      const key = "host-budget-stop"
      input.store.record(input.id, key, { schema: "drug-design-budget-stop-v1", based_on_revision: snapshot.loop.revision,
        decisions, model_calls: calls, now, deadline: snapshot.loop.created + snapshot.loop.contract.limits.max_elapsed_ms,
        ledger: snapshot.loop.ledger }, now)
      return input.store.finish(input.id, input.store.get(input.id).revision, "budget_exhausted", [key], now)
    }).immediate()
    return { status: "budget_exhausted" as const, invoked: false, loop }
  }
  const call = await runBudgetedResearchDecision({ store: input.store, id: input.id, key: input.key, policy: input.policy,
    request: { ...input.request, context: snapshot.context, payload: snapshot.payload } })
  const result = await recoverResearchLiveDecision(input)
  return { ...result, invoked: call.invoked }
}
