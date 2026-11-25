import { afterEach, describe, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, researchDigest, type ResearchContract } from "./research-loop-contract"
import { normalizeResearchModelUsage, readResearchModelCall, reserveResearchModelCall,
  completeResearchModelCall, failResearchModelCall, type ResearchModelRequest,
  type ResearchModelBudgetPolicy, type ResearchProviderBinding } from "./research-model-budget"

// These synthetic endpoint/usage receipts exercise host accounting only. They
// are not model calls, model-quality evaluations, or real provider price claims.
const hash = researchDigest("model-budget-test-fixture")
const fixtures: { directory: string; store: ResearchLoopStore }[] = []
afterEach(() => { for (const fixture of fixtures.splice(0)) { fixture.store.close(); rmSync(fixture.directory, { recursive: true, force: true }) } })

function fixture() {
  const directory = mkdtempSync(path.join(os.tmpdir(), "research-model-budget-"))
  const filename = path.join(directory, "research.sqlite")
  const store = new ResearchLoopStore(filename)
  fixtures.push({ directory, store })
  const contract: ResearchContract = {
    schema: "drug-design-research-contract-v1", directory, project: "fixture", session: "session-test",
    objective: "Test host model accounting only", mode: "offline-replay", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: null, mapping_sha256: null },
    inputs: { library: "fixture", library_sha256: null, subset_sha256: null, science_config_sha256: null, source_release: hash },
    quality: { manifest_sha256: null, required_checks: ["identity"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { ...emptyResources(), model_tokens: 300000, model_cost: 10 },
      max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
      max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 6, max_elapsed_ms: 60000 },
    allowed_adaptations: ["inspect", "stop"],
  }
  const loop = store.create(contract)
  store.record(loop.id, "fixture:budget-evidence", { scope: "synthetic fixture; not a public tariff", endpoint: "https://example.invalid/v1" })
  const policy: ResearchModelBudgetPolicy = {
    schema: "research-model-budget-policy-v1", provider_id: "fixture", model_id: "fixture-model", api_model_id: "fixture-model",
    endpoint_origin: "https://example.invalid", endpoint_base_url: "https://example.invalid/v1",
    response_model_ids: ["fixture-model"], input_token_cap: 100000, output_token_cap: 1024,
    input_cap_basis: "provider-enforced-context-window", output_cap_basis: "provider-enforced-including-reasoning",
    usage_semantics: "inclusive-input-output", pricing: { currency: "USD", basis: "conservative-price-ceiling",
      input_per_million: 10, output_per_million: 20, valid_until: new Date(Date.now() + 86400000).toISOString() },
    evidence_refs: ["fixture:budget-evidence"],
  }
  const binding: ResearchProviderBinding = { provider_id: "fixture", model_id: "fixture-model", api_model_id: "fixture-model",
    endpoint_origin: policy.endpoint_origin, endpoint_base_url: policy.endpoint_base_url,
    context_token_limit: 100000, output_token_limit: 1024 }
  const request = (): ResearchModelRequest => {
    const current = store.get(loop.id)
    return {
      user: { id: "user-test", role: "user", sessionID: contract.session, agent: "research", effort: "normal",
        time: { created: loop.created }, model: { providerID: "fixture", modelID: "fixture-model" } },
      sessionID: contract.session,
      agent: { name: "research", mode: "primary", permission: [], options: {} },
      model: { id: "fixture-model", providerID: "fixture", name: "fixture", api: { id: "fixture-model", url: policy.endpoint_base_url, npm: "@ai-sdk/openai-compatible" },
        capabilities: { temperature: true, reasoning: true, attachment: false, toolcall: true,
          input: { text: true, audio: false, image: false, video: false, pdf: false },
          output: { text: true, audio: false, image: false, video: false, pdf: false }, interleaved: false },
        // Zero defaults in the provider registry must not be treated as a free tariff.
        cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
        limit: { context: 100000, output: 1024 }, status: "active", options: {}, headers: {}, release_date: "fixture" },
      system: ["Fixture core instructions"], abort: new AbortController().signal,
      context: { loop_id: loop.id, state_revision: current.revision,
        evidence_refs: store.evidence(loop.id).map(row => row.key), capabilities: [], allowed_actions: [{ action: "finish" }] },
      payload: { research_contract: current.contract, research_state: { id: loop.id, revision: current.revision, state: current.state },
        resource_ledger: current.ledger, observations: [] },
      limits: { max_output_tokens: 100, timeout_ms: 1000 },
    }
  }
  const input = (key = "call-one") => ({ store, id: loop.id, key, policy, binding, request: request() })
  const prepare = (key = "call-one") => {
    const prepared = reserveResearchModelCall(input(key))
    if (prepared.kind !== "prepared") throw Error("Expected a new fixture claim")
    return prepared
  }
  return { directory, filename, store, loop, request, policy, binding, input, prepare }
}

function response(prepared: Extract<ReturnType<typeof reserveResearchModelCall>, { kind: "prepared" }>,
  usage: { inputTokens: number | undefined; outputTokens: number | undefined; totalTokens: number | undefined; cachedInputTokens?: number; reasoningTokens?: number }
    = { inputTokens: 80, outputTokens: 20, totalTokens: 100 }) {
  const decision = { schema: "drug-design-decision-v1" as const, loop_id: prepared.attempt.loop_id,
    state_revision: prepared.attempt.prepared_revision, action: "finish" as const, capability_id: null,
    plan_id: null, arguments: {}, evidence_refs: ["fixture:budget-evidence"],
    rationale: "Synthetic accounting fixture; no science claim.", alternatives: [], expected_observation: null,
    outcome: "insufficient_evidence" as const, missing_inputs: [] }
  return { text: JSON.stringify(decision), usage, finish_reason: "stop" as const,
    model: { providerID: "fixture", modelID: "fixture-model", response_model_id: "fixture-model" },
    prompt_sha256: prepared.attempt.prepared_prompt.sha256, prompt_hash_scope: "domain-and-decision-context" as const,
    validated: true as const, decision }
}

describe("strict provider usage", () => {
  test("keeps absent breakdowns unknown while counting inclusive totals once", () => {
    const result = normalizeResearchModelUsage({ inputTokens: 100, outputTokens: 50, totalTokens: 150 })
    expect(result).toEqual({ complete: true, usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150,
      cached_input_tokens: null, reasoning_tokens: null } })
    expect(normalizeResearchModelUsage({ inputTokens: 100, outputTokens: 50, totalTokens: 150, reasoningTokens: 40, cachedInputTokens: 70 }))
      .toEqual({ complete: true, usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150,
        cached_input_tokens: 70, reasoning_tokens: 40 } })
  })
  test.each([
    {}, { inputTokens: 1, outputTokens: 1 }, { inputTokens: undefined, outputTokens: 1, totalTokens: 1 },
    { inputTokens: null, outputTokens: 1, totalTokens: 1 }, { inputTokens: -1, outputTokens: 1, totalTokens: 0 },
    { inputTokens: Infinity, outputTokens: 1, totalTokens: Infinity }, { inputTokens: 1.1, outputTokens: 1, totalTokens: 2.1 },
    { inputTokens: 1, outputTokens: 1, totalTokens: 3 },
    { inputTokens: 1, outputTokens: 1, totalTokens: 2, reasoningTokens: 2 },
    { inputTokens: 1, outputTokens: 1, totalTokens: 2, cachedInputTokens: 2 },
  ])("does not convert incomplete or incompatible receipt %# into zero usage", (value) => {
    expect(normalizeResearchModelUsage(value).complete).toBe(false)
  })
  test("explicit zero usage is distinct from missing usage", () => {
    expect(normalizeResearchModelUsage({ inputTokens: 0, outputTokens: 0, totalTokens: 0 }).complete).toBe(true)
  })
})

describe("durable invocation and hard reservation", () => {
  test("reserves the whole enforced context plus bounded output before the call", () => {
    const item = fixture()
    const before = item.store.get(item.loop.id)
    const prepared = item.prepare()
    const current = item.store.get(item.loop.id)
    expect(current.ledger.reserved.model_tokens).toBe(100100)
    expect(current.ledger.reserved.model_cost).toBeCloseTo(1.002)
    expect(prepared.attempt.prepared_revision).toBe(before.revision + 2)
    expect(prepared.request.context.state_revision).toBe(current.revision)
    expect(prepared.request.payload.resource_ledger).toEqual(current.ledger)
    expect(JSON.parse(prepared.attempt.prepared_prompt.user).runtime_identity.state_revision).toBe(current.revision)
    expect(item.store.evidence(item.loop.id, "host").some(row => row.key === "model-attempt:call-one")).toBe(true)
  })

  test("durable claim replay never permits a second invocation, even through a new connection", () => {
    const item = fixture()
    const input = item.input()
    const first = reserveResearchModelCall(input)
    const second = new ResearchLoopStore(item.filename)
    try {
      const repeat = reserveResearchModelCall({ ...input, store: second })
      expect(first.kind).toBe("prepared")
      expect(repeat.kind).toBe("existing")
      if (repeat.kind === "existing") {
        expect(repeat.call.status).toBe("in_flight_or_usage_unknown")
        expect(repeat.call.can_apply).toBe(false)
      }
      expect(second.reservations(item.loop.id)).toHaveLength(1)
      expect(() => reserveResearchModelCall({ ...input, store: second,
        request: { ...input.request, limits: { ...input.request.limits, max_output_tokens: 99 } } })).toThrow("different request")
    } finally { second.close() }
  })

  test("another key cannot retry unresolved consumption and a small budget cannot use an estimate", () => {
    const item = fixture()
    item.prepare()
    expect(() => reserveResearchModelCall(item.input("call-two"))).toThrow("unsettled")
    expect(item.store.get(item.loop.id).ledger.total.model_tokens).toBe(100100)
    const scarce = { ...item.loop.contract, limits: { ...item.loop.contract.limits,
      resources: { ...item.loop.contract.limits.resources, model_tokens: 50000 } } }
    const loop = item.store.create(scarce)
    item.store.record(loop.id, "fixture:budget-evidence", { fixture: true })
    const current = item.store.get(loop.id)
    const original = item.request()
    const request = { ...original, context: { ...original.context, loop_id: loop.id, state_revision: current.revision, evidence_refs: ["fixture:budget-evidence"] },
      payload: { ...original.payload, research_contract: current.contract, resource_ledger: current.ledger,
        research_state: { id: loop.id, revision: current.revision } } }
    expect(() => reserveResearchModelCall({ ...item.input(), id: loop.id, request })).toThrow("Cumulative research budget")
    expect(item.store.reservations(loop.id)).toEqual([])
  })

  test("endpoint, source, price expiry, limits and hidden tier routing are checked before reservation", () => {
    const item = fixture()
    expect(() => reserveResearchModelCall({ ...item.input(), binding: { ...item.binding, endpoint_base_url: "https://other.invalid/v1", endpoint_origin: "https://other.invalid" } })).toThrow("endpoint")
    expect(() => reserveResearchModelCall({ ...item.input(), policy: { ...item.policy, evidence_refs: ["invented-source"] } })).toThrow("evidence")
    expect(() => reserveResearchModelCall({ ...item.input(), policy: { ...item.policy, pricing: { ...item.policy.pricing, valid_until: new Date(0).toISOString() } } })).toThrow("expired")
    expect(() => reserveResearchModelCall({ ...item.input(), policy: { ...item.policy, input_token_cap: 50000 } })).toThrow("context limits")
    const routed = item.request()
    routed.user.tier = "different-model"
    expect(() => reserveResearchModelCall({ ...item.input(), request: routed })).toThrow("tier or variant")
    const excessive = item.request()
    excessive.limits.max_output_tokens = 2048
    expect(() => reserveResearchModelCall({ ...item.input(), request: excessive })).toThrow("output or timeout")
    expect(item.store.reservations(item.loop.id)).toEqual([])
  })

  test("request context, ledger and evidence must belong to the exact host state", () => {
    const item = fixture()
    const request = item.request()
    item.store.record(item.loop.id, "late:host", { value: 1 })
    expect(() => reserveResearchModelCall({ ...item.input(), request })).toThrow("stale")
    const altered = item.request()
    altered.payload.resource_ledger = { reset: true }
    expect(() => reserveResearchModelCall({ ...item.input(), request: altered })).toThrow("ledger")
    const evidence = item.request()
    evidence.context.evidence_refs = ["fixture:budget-evidence"]
    expect(() => reserveResearchModelCall({ ...item.input(), request: evidence })).toThrow("evidence set")
  })
})

describe("receipt accounting and decision CAS", () => {
  test("settles complete real-format usage at the frozen price ceiling, preserving decision provenance", () => {
    const item = fixture()
    const prepared = item.prepare()
    const result = response(prepared, { inputTokens: 80, outputTokens: 20, totalTokens: 100, cachedInputTokens: 60, reasoningTokens: 15 })
    const call = completeResearchModelCall({ ...item.input(), result })
    expect(call.status).toBe("completed")
    expect(call.can_apply).toBe(true)
    expect(call.receipt?.pre_state_digest).toBe(prepared.attempt.pre_state_digest)
    expect(call.receipt?.accounting.model_cost_upper_bound).toBeCloseTo(0.0012)
    expect(item.store.get(item.loop.id).ledger.used.model_tokens).toBe(100)
    expect(item.store.get(item.loop.id).ledger.reserved).toEqual(emptyResources())
    expect(call.expected_revision).toBe(item.store.get(item.loop.id).revision)
    const repeated = completeResearchModelCall({ ...item.input(), result })
    expect(repeated.expected_revision).toBe(call.expected_revision)
    item.store.decide(item.loop.id, call.expected_revision!, { result: result.decision, receipt: call.receipt_ref })
    expect(readResearchModelCall(item.store, item.loop.id, "call-one")?.can_apply).toBe(false)
    expect(() => item.store.decide(item.loop.id, call.expected_revision!, result.decision)).toThrow("Stale")
  })

  test("invalid decision JSON still incurs its usage and cannot be applied", () => {
    const item = fixture()
    const prepared = item.prepare()
    const valid = response(prepared)
    const { decision, ...fields } = valid
    const result = { ...fields, text: "not valid JSON", validated: false as const, error: "Invalid JSON" }
    const call = completeResearchModelCall({ ...item.input(), result })
    expect(call.status).toBe("completed")
    expect(call.can_apply).toBe(false)
    expect(call.receipt?.decision_validated).toBe(false)
    expect(item.store.get(item.loop.id).ledger.used.model_tokens).toBe(100)
  })

  test("missing usage or model mismatch retains the whole reservation and blocks further calls", () => {
    for (const mismatch of [false, true]) {
      const item = fixture()
      const prepared = item.prepare()
      const result = mismatch ? { ...response(prepared), model: { providerID: "fixture", modelID: "fixture-model", response_model_id: "unpriced-model" } }
        : response(prepared, { inputTokens: undefined, outputTokens: 20, totalTokens: undefined })
      const call = completeResearchModelCall({ ...item.input(), result })
      expect(call.status).toBe("usage_unresolved")
      expect(call.can_apply).toBe(false)
      expect(call.receipt?.accounting.usage).toBeNull()
      expect(item.store.get(item.loop.id).ledger.reserved.model_tokens).toBe(100100)
      expect(item.store.get(item.loop.id).state).toBe("BLOCKED")
      expect(() => reserveResearchModelCall(item.input("call-two"))).toThrow("Blocked")
    }
  })

  test("timeout/transport uncertainty is durable and cannot be overwritten by a fabricated zero receipt", () => {
    const item = fixture()
    const prepared = item.prepare()
    const call = failResearchModelCall(item.input())
    expect(call.status).toBe("transport_unresolved")
    expect(call.receipt?.accounting.state).toBe("unresolved")
    expect(item.store.get(item.loop.id).ledger.unsettled).toEqual(["call-one"])
    expect(failResearchModelCall(item.input()).expected_revision).toBe(call.expected_revision)
    expect(() => completeResearchModelCall({ ...item.input(), result: response(prepared, { inputTokens: 0, outputTokens: 0, totalTokens: 0 }) })).toThrow("immutable")
  })

  test("external state changes and cancellation invalidate the proposal but still charge actual usage", () => {
    for (const cancel of [false, true]) {
      const item = fixture()
      const prepared = item.prepare()
      if (cancel) item.store.finish(item.loop.id, item.store.get(item.loop.id).revision, "user_cancelled", [])
      else item.store.record(item.loop.id, "new:observation-metadata", { changed: true })
      const call = completeResearchModelCall({ ...item.input(), result: response(prepared) })
      expect(call.can_apply).toBe(false)
      expect(call.receipt?.state_changed_during_call).toBe(true)
      expect(item.store.get(item.loop.id).ledger.used.model_tokens).toBe(100)
      if (cancel) expect(item.store.get(item.loop.id).state).toBe("CANCELLED")
    }
  })

  test("repeated calls accumulate usage instead of resetting the research budget", () => {
    const item = fixture()
    const first = item.prepare()
    completeResearchModelCall({ ...item.input(), result: response(first, { inputTokens: 10000, outputTokens: 100, totalTokens: 10100 }) })
    const second = item.prepare("call-two")
    expect(item.store.get(item.loop.id).ledger.total.model_tokens).toBe(110200)
    completeResearchModelCall({ ...item.input("call-two"), result: response(second, { inputTokens: 12000, outputTokens: 50, totalTokens: 12050 }) })
    expect(item.store.get(item.loop.id).ledger.used.model_tokens).toBe(22150)
    expect(item.store.get(item.loop.id).ledger.used.model_cost).toBeCloseTo(0.223)
  })

  test("an output cap violation is recorded and blocks reuse of the failed endpoint guarantee", () => {
    const item = fixture()
    const prepared = item.prepare()
    const call = completeResearchModelCall({ ...item.input(), result: response(prepared, { inputTokens: 80, outputTokens: 200, totalTokens: 280 }) })
    expect(call.can_apply).toBe(false)
    expect(call.receipt?.bounds_violated).toEqual(["output_token_cap"])
    expect(item.store.get(item.loop.id).ledger.used.model_tokens).toBe(280)
    expect(item.store.get(item.loop.id).state).toBe("BLOCKED")
    item.store.observe(item.loop.id, "late:observation", { source: "fixture" })
    expect(() => reserveResearchModelCall(item.input("call-two"))).toThrow("guarantee was violated")
  })
})
