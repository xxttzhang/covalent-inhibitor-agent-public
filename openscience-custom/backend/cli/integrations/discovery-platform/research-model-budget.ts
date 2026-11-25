import z from "zod"
import { buildResearchDecisionPrompt, ResearchDecision, requestResearchDecision } from "./research-decision"
import { emptyResources, researchDigest, researchHash, resourceKeys } from "./research-loop-contract"
import { ResearchLoopStore } from "./research-loop-store"

const text = z.string().min(1)
const tokens = z.number().int().nonnegative()
const cap = z.number().int().positive()

/** These are host-verified endpoint guarantees, not model-authored estimates.
 * The entire enforced context window is reserved, including core/plugin input
 * that this module cannot tokenize before LLM.stream assembles the request. */
export const ResearchModelBudgetPolicy = z.strictObject({
  schema: z.literal("research-model-budget-policy-v1"),
  provider_id: text, model_id: text, api_model_id: text,
  endpoint_origin: z.string().url(), endpoint_base_url: z.string().url(),
  response_model_ids: z.array(text).min(1),
  input_token_cap: cap, output_token_cap: cap,
  input_cap_basis: z.literal("provider-enforced-context-window"),
  output_cap_basis: z.literal("provider-enforced-including-reasoning"),
  usage_semantics: z.literal("inclusive-input-output"),
  pricing: z.union([z.strictObject({ currency: z.literal("USD"), basis: z.literal("conservative-price-ceiling"),
    input_per_million: z.number().finite().nonnegative(), output_per_million: z.number().finite().nonnegative(),
    valid_until: z.string().datetime() }), z.strictObject({
    basis: z.literal("user-authorized-unbounded"), currency: z.null(),
    input_per_million: z.null(), output_per_million: z.null(),
    authorization_ref: text, valid_until: z.string().datetime(),
  })]),
  evidence_refs: z.array(text).min(1),
}).superRefine((value, context) => {
  if (new Set(value.response_model_ids).size !== value.response_model_ids.length
    || new Set(value.evidence_refs).size !== value.evidence_refs.length)
    context.addIssue({ code: "custom", message: "Policy references must be unique" })
  const endpoint = new URL(value.endpoint_base_url)
  if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
    || endpoint.origin !== value.endpoint_origin)
    context.addIssue({ code: "custom", message: "Policy requires an exact HTTPS endpoint and origin without embedded credentials or queries" })
})
export type ResearchModelBudgetPolicy = z.infer<typeof ResearchModelBudgetPolicy>
export type ResearchModelRequest = Parameters<typeof requestResearchDecision>[0]

export type ResearchProviderBinding = {
  provider_id: string; model_id: string; api_model_id: string
  endpoint_origin: string; endpoint_base_url: string
  context_token_limit: number; output_token_limit: number
}

const bindingSchema = z.strictObject({
  provider_id: text, model_id: text, api_model_id: text,
  endpoint_origin: z.string().url(), endpoint_base_url: z.string().url(),
  context_token_limit: cap, output_token_limit: cap,
})
const attemptSchema = z.strictObject({
  schema: z.literal("research-model-attempt-v1"), loop_id: text, key: text,
  request_digest: researchHash, pre_state_digest: researchHash, reservation_plan_digest: researchHash,
  base_revision: tokens, prepared_revision: tokens,
  policy: ResearchModelBudgetPolicy, binding: bindingSchema,
  prepared_prompt: z.strictObject({ system: text, user: text, sha256: researchHash }),
  max_output_tokens: cap, timeout_ms: cap, created_at: tokens,
})
export type ResearchModelAttempt = z.infer<typeof attemptSchema>

export type ResearchModelUsage = {
  input_tokens: number; output_tokens: number; total_tokens: number
  cached_input_tokens: number | null; reasoning_tokens: number | null
}
export type ResearchModelReceipt = {
  schema: "research-model-receipt-v1"; loop_id: string; key: string
  request_digest: string; pre_state_digest: string; decision_context_sha256: string
  base_revision: number; prepared_revision: number; expected_revision: number
  status: "completed" | "usage_unresolved" | "transport_unresolved"
  state_changed_during_call: boolean; decision_validated: boolean; can_apply: boolean
  bounds_violated: string[]
  accounting: { state: "settled" | "usage-settled-cost-unknown" | "unresolved"; usage: ResearchModelUsage | null
    model_cost_upper_bound: number | null; currency: "USD" | null; pricing_basis: "conservative-price-ceiling" | "user-authorized-unbounded"; reason: string | null }
  result: Awaited<ReturnType<typeof requestResearchDecision>> | null
  failure: { kind: "transport_or_abort"; message: string } | null
}

const canonicalURL = (url: string) => new URL(url).href.replace(/\/$/, "")
const attemptRef = (key: string) => `model-attempt:${key}`
const receiptRef = (key: string) => `model-receipt:${key}`
const terminal = (state: string) => state === "FINISHED" || state === "CANCELLED"
export const researchModelCost = (input: number, output: number, policy: ResearchModelBudgetPolicy) => {
  if (policy.pricing.basis === "user-authorized-unbounded") return null
  const value = (input * policy.pricing.input_per_million + output * policy.pricing.output_per_million) / 1_000_000
  if (!Number.isFinite(value)) throw Error("Model price ceiling is not finite")
  // Round upward rather than understating the contractual cost ceiling.
  return Math.ceil(value * 1e12) / 1e12
}

/** Missing totals are unknown, never zero. Optional breakdowns may remain null
 * because the verified policy states that their tokens are included in totals. */
export function normalizeResearchModelUsage(input: unknown):
  { complete: true; usage: ResearchModelUsage } | { complete: false; reason: string } {
  const parsed = z.object({ inputTokens: tokens, outputTokens: tokens, totalTokens: tokens,
    cachedInputTokens: tokens.nullable().optional(), reasoningTokens: tokens.nullable().optional() }).safeParse(input)
  if (!parsed.success) return { complete: false, reason: "Provider input/output/total usage is missing or invalid" }
  const value = parsed.data
  if (!Number.isSafeInteger(value.inputTokens + value.outputTokens) || value.totalTokens !== value.inputTokens + value.outputTokens)
    return { complete: false, reason: "Provider totals do not match the verified inclusive input/output semantics" }
  if ((value.cachedInputTokens ?? 0) > value.inputTokens || (value.reasoningTokens ?? 0) > value.outputTokens)
    return { complete: false, reason: "Provider token breakdown exceeds its inclusive total" }
  return { complete: true, usage: { input_tokens: value.inputTokens, output_tokens: value.outputTokens,
    total_tokens: value.totalTokens, cached_input_tokens: value.cachedInputTokens ?? null,
    reasoning_tokens: value.reasoningTokens ?? null } }
}

function validate(input: { store: ResearchLoopStore; id: string; request: ResearchModelRequest;
  policy: ResearchModelBudgetPolicy; binding: ResearchProviderBinding }, now: number) {
  const policy = ResearchModelBudgetPolicy.parse(input.policy)
  const binding = bindingSchema.parse(input.binding)
  const request = input.request
  if (request.user.tier !== undefined || request.user.variant !== undefined)
    throw Error("Budgeted research does not permit unresolved tier or variant routing")
  for (const options of [request.model.options, request.agent.options]) {
    if (["n", "num_return_sequences", "model", "models", "baseURL", "endpoint", "service_tier", "serviceTier"].some(key => options[key] !== undefined))
      throw Error("Budgeted research does not permit extra completions or unpriced request routing options")
  }
  if (request.model.providerID !== policy.provider_id || request.model.id !== policy.model_id
    || request.model.api.id !== policy.api_model_id || binding.provider_id !== policy.provider_id
    || binding.model_id !== policy.model_id || binding.api_model_id !== policy.api_model_id)
    throw Error("Model budget policy is not bound to the actual provider/model")
  if (canonicalURL(binding.endpoint_base_url) !== canonicalURL(policy.endpoint_base_url)
    || binding.endpoint_origin !== policy.endpoint_origin)
    throw Error("Model budget policy endpoint differs from the resolved provider endpoint")
  if (binding.context_token_limit !== policy.input_token_cap || request.model.limit.context !== policy.input_token_cap
    || binding.output_token_limit !== request.model.limit.output)
    throw Error("Model context limits differ from the verified server-enforced window")
  if (!Number.isSafeInteger(request.limits.max_output_tokens) || request.limits.max_output_tokens < 1
    || request.limits.max_output_tokens > policy.output_token_cap || request.limits.max_output_tokens > binding.output_token_limit
    || !Number.isSafeInteger(request.limits.timeout_ms) || request.limits.timeout_ms < 1 || request.limits.timeout_ms > 2_147_483_647)
    throw Error("Model request exceeds its verified output or timeout bound")
  if (Date.parse(policy.pricing.valid_until) <= now) throw Error("Model price ceiling has expired")
  const loop = input.store.get(input.id)
  if ((policy.pricing.basis === "user-authorized-unbounded") !== (loop.contract.limits.resources.model_cost === null))
    throw Error("Unbounded model pricing requires an explicitly uncapped contract; capped contracts require verified prices")
  if (policy.pricing.basis === "user-authorized-unbounded" && !policy.evidence_refs.includes(policy.pricing.authorization_ref))
    throw Error("Unbounded model pricing requires its user authorization evidence")
  if (request.sessionID !== loop.contract.session || request.user.sessionID !== request.sessionID
    || request.user.model.providerID !== policy.provider_id || request.user.model.modelID !== policy.model_id)
    throw Error("Model request is not bound to the research session and selected model")
  const evidence = input.store.evidence(input.id, "host")
  if (evidence.some(row => {
    const value = row.payload as Partial<ResearchModelReceipt>
    return value?.schema === "research-model-receipt-v1" && value.bounds_violated?.length
  })) throw Error("A verified model budget guarantee was violated; further calls require a new reviewed contract")
  if (policy.pricing.basis === "user-authorized-unbounded") {
    const ref = policy.pricing.authorization_ref
    const envelope = evidence.find(row => row.key === ref)?.payload as { content?: unknown } | undefined
    z.strictObject({ schema: z.literal("research-model-cost-authorization-v1"),
      origin: z.literal("explicit-user-instruction"), scope: z.literal("model-fees-only"),
      model_cost_limit: z.null(), source_ref: text, statement: text,
      recorded_at: z.string().datetime({ offset: true }),
    }).parse(envelope?.content)
  }
  const refs = new Set(evidence.map(row => row.key))
  if (policy.evidence_refs.some(ref => !refs.has(ref))) throw Error("Model budget policy lacks its host-verified endpoint, price and limit evidence")
  return { policy, binding, loop }
}

/** Public resolver reads the same configured provider state used by LLM.stream;
 * it never reads credentials or sends an inference request. Unknown custom
 * transport is rejected because its routing cannot be established here. */
export async function resolveResearchProviderBinding(request: ResearchModelRequest): Promise<ResearchProviderBinding> {
  const { Provider } = await import("../../src/provider/provider")
  const model = await Provider.getModel(request.model.providerID, request.model.id)
  const provider = await Provider.getProvider(model.providerID)
  if (!provider || provider.options.fetch !== undefined)
    throw Error("Budgeted research requires a known provider without a custom transport")
  const base = provider.options.baseURL || model.api.url
  if (typeof base !== "string") throw Error("Actual provider endpoint is unavailable")
  const endpoint = new URL(base)
  if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash)
    throw Error("Actual provider endpoint is not a credential-free HTTPS base URL")
  // The platform can rewrite Atlas proxy routes according to credential mode.
  // Such managed routing needs an end-of-routing binding, unavailable here.
  if (endpoint.hostname.endsWith("syntheticsciences.ai") || endpoint.pathname.includes("/llm/"))
    throw Error("Managed proxy endpoint requires a separately verified final routing binding")
  if (researchDigest({ provider: model.providerID, id: model.id, api: model.api, limit: model.limit, options: model.options, headers: model.headers })
    !== researchDigest({ provider: request.model.providerID, id: request.model.id, api: request.model.api, limit: request.model.limit,
      options: request.model.options, headers: request.model.headers }))
    throw Error("Configured provider model changed since the request was prepared")
  return { provider_id: model.providerID, model_id: model.id, api_model_id: model.api.id,
    endpoint_origin: endpoint.origin, endpoint_base_url: endpoint.href,
    context_token_limit: model.limit.context, output_token_limit: model.limit.output }
}

/** Read-only recovery never authorizes another provider request. A settled
 * receipt is applicable at most once, by a controller CAS on expected_revision. */
export function readResearchModelCall(store: ResearchLoopStore, id: string, key: string) {
  return store.db.transaction(() => {
    const evidence = store.evidence(id, "host")
    const row = evidence.find(item => item.key === attemptRef(key))
    if (!row) return null
    const attempt = attemptSchema.parse(row.payload)
    if (attempt.loop_id !== id || attempt.key !== key) throw Error("Model attempt identity mismatch")
    const reservation = store.reservations(id).find(item => item.key === key)
    if (!reservation || reservation.payload.kind !== "model" || reservation.payload.plan_digest !== attempt.reservation_plan_digest)
      throw Error("Model attempt has no matching frozen reservation")
    const receipt = evidence.find(item => item.key === receiptRef(key))?.payload as ResearchModelReceipt | undefined
    if (receipt && (receipt.schema !== "research-model-receipt-v1" || receipt.loop_id !== id || receipt.key !== key
      || receipt.request_digest !== attempt.request_digest || receipt.pre_state_digest !== attempt.pre_state_digest
      || receipt.decision_context_sha256 !== attempt.prepared_prompt.sha256))
      throw Error("Model receipt does not match its frozen attempt")
    const loop = store.get(id)
    return { attempt, receipt: receipt ?? null, attempt_ref: attemptRef(key), receipt_ref: receipt ? receiptRef(key) : null,
      status: receipt?.status ?? "in_flight_or_usage_unknown" as const, invoked: false,
      can_apply: Boolean(receipt?.can_apply && receipt.expected_revision === loop.revision && !terminal(loop.state)),
      expected_revision: receipt?.expected_revision ?? null }
  }).deferred()
}

/** This transaction is the durable invocation claim: no caller that finds an
 * existing attempt may call the provider again, including after a crash. */
export function reserveResearchModelCall(input: {
  store: ResearchLoopStore; id: string; key: string; request: ResearchModelRequest
  policy: ResearchModelBudgetPolicy; binding: ResearchProviderBinding; now?: number
}) {
  if (!input.key.trim()) throw Error("Model call requires an idempotency key")
  input.request.abort.throwIfAborted()
  const now = input.now ?? Date.now()
  return input.store.db.transaction(() => {
    const { policy, binding, loop } = validate(input, now)
    const initial = buildResearchDecisionPrompt(input.request.context, input.request.payload)
    const fingerprint = researchDigest({ prompt: initial.sha256, policy, binding,
      user: input.request.user, agent: input.request.agent, system: input.request.system,
      model_configuration: { options: input.request.model.options, headers: input.request.model.headers },
      limits: input.request.limits, session: input.request.sessionID })
    const prior = readResearchModelCall(input.store, input.id, input.key)
    if (prior) {
      if (prior.attempt.request_digest !== fingerprint) throw Error("Model call key reused for a different request")
      return { kind: "existing" as const, call: prior }
    }
    if (input.store.reservations(input.id).some(row => row.key === input.key))
      throw Error("Existing model reservation has uncertain provenance; do not retry")
    if (now + input.request.limits.timeout_ms > loop.created + loop.contract.limits.max_elapsed_ms)
      throw Error("Model timeout exceeds the remaining research wall-clock budget")
    if (input.request.context.loop_id !== input.id || input.request.context.state_revision !== loop.revision)
      throw Error("Model request is based on a stale research state")
    if (researchDigest(input.request.payload.research_contract) !== researchDigest(loop.contract)
      || researchDigest(input.request.payload.resource_ledger) !== researchDigest(loop.ledger))
      throw Error("Model request contract or ledger differs from host-owned state")
    const refs = input.store.evidence(input.id).map(row => row.key).sort()
    if (researchDigest([...input.request.context.evidence_refs].sort()) !== researchDigest(refs))
      throw Error("Model request evidence set differs from host-owned state")
    const state = z.object({ id: text, revision: tokens }).passthrough().parse(input.request.payload.research_state)
    if (state.id !== input.id || state.revision !== loop.revision) throw Error("Model request state payload is stale")
    const resources = { ...emptyResources(), model_tokens: policy.input_token_cap + input.request.limits.max_output_tokens,
      model_cost: researchModelCost(policy.input_token_cap, input.request.limits.max_output_tokens, policy) ?? 0 }
    if (!Number.isSafeInteger(resources.model_tokens)) throw Error("Model token reservation is not a safe integer")
    const prepared = loop.revision + 2
    const ledger = { ...loop.ledger, used: { ...loop.ledger.used }, reserved: { ...loop.ledger.reserved },
      total: { ...loop.ledger.total }, unsettled: [...loop.ledger.unsettled, input.key] }
    for (const key of resourceKeys) { ledger.reserved[key] += resources[key]; ledger.total[key] += resources[key] }
    if (ledger.model_cost_accounting) ledger.model_cost_accounting = { ...ledger.model_cost_accounting,
      unpriced_calls: [...ledger.model_cost_accounting.unpriced_calls, input.key] }
    const request: ResearchModelRequest = { ...input.request,
      context: { ...input.request.context, state_revision: prepared },
      payload: { ...input.request.payload, research_state: { ...state, revision: prepared }, resource_ledger: ledger } }
    const prompt = buildResearchDecisionPrompt(request.context, request.payload)
    const events = input.store.events(input.id)
    const previous = researchDigest({ id: input.id, revision: loop.revision, state: loop.state, outcome: loop.outcome,
      contract_digest: loop.digest, ledger: loop.ledger,
      evidence: input.store.evidence(input.id).map(row => ({ key: row.key, kind: row.kind, sha256: row.digest, revision: row.revision })),
      event_head: events.at(-1)?.digest ?? null })
    const plan = researchDigest({ request_digest: fingerprint, pre_state_digest: previous,
      prepared_prompt_sha256: prompt.sha256, prepared_revision: prepared, resources })
    const attempt = attemptSchema.parse({ schema: "research-model-attempt-v1", loop_id: input.id, key: input.key,
      request_digest: fingerprint, pre_state_digest: previous, reservation_plan_digest: plan,
      base_revision: loop.revision, prepared_revision: prepared, policy, binding, prepared_prompt: prompt,
      max_output_tokens: request.limits.max_output_tokens, timeout_ms: request.limits.timeout_ms, created_at: now })
    input.store.reserve(input.id, loop.revision, { key: input.key, kind: "model", round: 0, candidate_slots: 0,
      plan_digest: plan, source_release: loop.contract.inputs.source_release, resources }, now)
    input.store.record(input.id, attemptRef(input.key), attempt, now)
    if (input.store.get(input.id).revision !== prepared) throw Error("Unexpected model preparation revision")
    return { kind: "prepared" as const, attempt, request, attempt_ref: attemptRef(input.key) }
  }).immediate()
}

/** Called only with the actual LLM receipt by the runtime, never as a model tool.
 * Invalid JSON still consumes tokens. Unknown usage keeps the entire reservation. */
export function completeResearchModelCall(input: { store: ResearchLoopStore; id: string; key: string;
  result: Awaited<ReturnType<typeof requestResearchDecision>>; now?: number }) {
  return finalize(input, input.result, null)
}

/** A transport error does not prove that the provider performed no work. */
export function failResearchModelCall(input: { store: ResearchLoopStore; id: string; key: string; now?: number }) {
  return finalize(input, null, { kind: "transport_or_abort", message: "Provider request failed or was aborted; billed usage is unresolved and the reservation remains held" })
}

function finalize(input: { store: ResearchLoopStore; id: string; key: string; now?: number },
  result: Awaited<ReturnType<typeof requestResearchDecision>> | null, failure: ResearchModelReceipt["failure"]) {
  return input.store.db.transaction(() => {
    const prior = readResearchModelCall(input.store, input.id, input.key)
    if (!prior) throw Error("No durable model invocation claim exists")
    if (prior.receipt) {
      if (researchDigest(prior.receipt.result) !== researchDigest(result) || researchDigest(prior.receipt.failure) !== researchDigest(failure))
        throw Error("Model receipt is immutable; reconcile unknown usage explicitly instead of overwriting it")
      return prior
    }
    const attempt = prior.attempt
    const current = input.store.get(input.id)
    const normalized = normalizeResearchModelUsage(result?.usage)
    const source = result?.prompt_sha256 === attempt.prepared_prompt.sha256
      && result.model.providerID === attempt.policy.provider_id && result.model.modelID === attempt.policy.model_id
      && attempt.policy.response_model_ids.includes(result.model.response_model_id)
    const complete = normalized.complete && Boolean(source)
    const usage = complete && normalized.complete ? normalized.usage : null
    const charge = usage ? researchModelCost(usage.input_tokens, usage.output_tokens, attempt.policy) : null
    const changed = current.revision !== attempt.prepared_revision || terminal(current.state)
    const parsed = result?.validated ? ResearchDecision.safeParse(result.decision) : null
    const valid = Boolean(parsed?.success && parsed.data.loop_id === input.id
      && parsed.data.state_revision === attempt.prepared_revision)
    const bounds = usage ? [
      ...(usage.input_tokens > attempt.policy.input_token_cap ? ["input_token_cap"] : []),
      ...(usage.output_tokens > attempt.max_output_tokens ? ["output_token_cap"] : []),
    ] : []
    const block = (!complete || bounds.length > 0) && !terminal(current.state)
    const receipt: ResearchModelReceipt = { schema: "research-model-receipt-v1", loop_id: input.id, key: input.key,
      request_digest: attempt.request_digest, pre_state_digest: attempt.pre_state_digest,
      decision_context_sha256: attempt.prepared_prompt.sha256,
      base_revision: attempt.base_revision, prepared_revision: attempt.prepared_revision,
      expected_revision: current.revision + 1 + (complete ? 1 : 0) + (block ? 1 : 0),
      status: failure ? "transport_unresolved" : complete ? "completed" : "usage_unresolved",
      state_changed_during_call: changed, decision_validated: valid,
      can_apply: complete && valid && !changed && bounds.length === 0, bounds_violated: bounds,
      accounting: { state: complete ? charge === null ? "usage-settled-cost-unknown" : "settled" : "unresolved", usage, model_cost_upper_bound: charge,
        currency: attempt.policy.pricing.currency, pricing_basis: attempt.policy.pricing.basis,
        reason: complete ? null : !source && result ? "Provider/model/context receipt binding mismatch"
          : normalized.complete ? "Provider transport did not supply a bound complete receipt" : normalized.reason },
      result, failure }
    input.store.record(input.id, receiptRef(input.key), receipt, input.now)
    if (usage) input.store.settle(input.id, input.key,
      { ...emptyResources(), model_tokens: usage.total_tokens, model_cost: charge ?? 0 }, receiptRef(input.key), input.now)
    if (block) input.store.transition(input.id, input.store.get(input.id).revision, "BLOCKED",
      { reason: bounds.length ? "model_budget_guarantee_violated" : "model_usage_unresolved", receipt: receiptRef(input.key) }, input.now)
    return readResearchModelCall(input.store, input.id, input.key)!
  }).immediate()
}

/** The sole convenience driver for real inference. No injected fake provider,
 * no automatic retry, no credentials, and no direct SDK bypass are used here. */
export async function runBudgetedResearchDecision(input: { store: ResearchLoopStore; id: string; key: string;
  request: ResearchModelRequest; policy: ResearchModelBudgetPolicy }) {
  const binding = await resolveResearchProviderBinding(input.request)
  const prepared = reserveResearchModelCall({ ...input, binding })
  if (prepared.kind === "existing") return prepared.call
  // Construct the final-send policy from the persisted attempt, not mutable
  // provider configuration or a caller-supplied requestBoundary.
  const frozen = prepared.attempt
  const result = await requestResearchDecision({ ...prepared.request, requestBoundary: {
    providerID: frozen.binding.provider_id, modelID: frozen.binding.model_id, apiModelID: frozen.binding.api_model_id,
    endpointOrigin: frozen.policy.endpoint_origin, endpointBaseURL: frozen.policy.endpoint_base_url,
    contextTokenLimit: frozen.binding.context_token_limit, outputTokenLimit: frozen.binding.output_token_limit,
    maxOutputTokens: frozen.max_output_tokens,
  } }).catch(() => null)
  const call = result ? completeResearchModelCall({ ...input, result }) : failResearchModelCall(input)
  return { ...call, invoked: true, prepared_context: prepared.request.context, prepared_payload: prepared.request.payload }
}
