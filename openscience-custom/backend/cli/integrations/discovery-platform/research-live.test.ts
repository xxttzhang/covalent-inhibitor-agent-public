import { afterEach, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, researchDigest, type ResearchContract } from "./research-loop-contract"
import { researchSnapshot } from "./research-loop"
import { guardResearchAction } from "./research-action-guard"
import { createResearchObservation, captureResearchArtifact } from "./research-observations"
import { reserveResearchModelCall, completeResearchModelCall, failResearchModelCall,
  type ResearchModelRequest, type ResearchModelBudgetPolicy, type ResearchProviderBinding } from "./research-model-budget"
import { recoverResearchLiveDecision, runResearchLiveDecision } from "./research-live"

// Synthetic durable provider receipts test recovery and CAS, not LLM quality.
// Every runResearchLiveDecision call below has an old claim or an aborted signal;
// no fixture resolves a provider, invokes a model, or submits remote computation.
const fixtures: { directory: string; store: ResearchLoopStore }[] = []
afterEach(() => { for (const item of fixtures.splice(0)) { item.store.close(); rmSync(item.directory, { recursive: true, force: true }) } })
const hash = researchDigest("live-controller-fixture")

function fixture(limits: Partial<ResearchContract["limits"]> = {}, now = Date.now()) {
  const directory = mkdtempSync(path.join(os.tmpdir(), "research-live-"))
  const filename = path.join(directory, "loop.sqlite")
  const store = new ResearchLoopStore(filename)
  fixtures.push({ directory, store })
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "fixture", session: "fixture-session",
    objective: "Exercise decision recovery without inference or chemistry", mode: "offline-replay", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A", author_residue: 773,
      reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: null, mapping_sha256: null },
    inputs: { library: "fixture", library_sha256: null, subset_sha256: null, science_config_sha256: null, source_release: hash },
    quality: { manifest_sha256: null, required_checks: ["identity"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { ...emptyResources(), model_tokens: 300000, model_cost: 10 }, max_library_records: 20000, max_rounds: 2,
      max_candidates_per_round: 5, max_total_candidates: 10, max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25,
      max_decisions: 6, max_elapsed_ms: 60000, ...limits }, allowed_adaptations: ["inspect", "stop"] }
  const loop = store.create(contract, now)
  store.record(loop.id, "fixture:policy", { scope: "Synthetic endpoint guarantee fixture; not a real provider tariff" })
  const policy: ResearchModelBudgetPolicy = { schema: "research-model-budget-policy-v1", provider_id: "fixture", model_id: "fixture-model",
    api_model_id: "fixture-model", endpoint_origin: "https://example.invalid", endpoint_base_url: "https://example.invalid/v1",
    response_model_ids: ["fixture-model"], input_token_cap: 100000, output_token_cap: 1024,
    input_cap_basis: "provider-enforced-context-window", output_cap_basis: "provider-enforced-including-reasoning",
    usage_semantics: "inclusive-input-output", pricing: { currency: "USD", basis: "conservative-price-ceiling",
      input_per_million: 10, output_per_million: 20, valid_until: new Date(Date.now() + 86400000).toISOString() }, evidence_refs: ["fixture:policy"] }
  const binding: ResearchProviderBinding = { provider_id: "fixture", model_id: "fixture-model", api_model_id: "fixture-model",
    endpoint_origin: policy.endpoint_origin, endpoint_base_url: policy.endpoint_base_url, context_token_limit: 100000, output_token_limit: 1024 }
  const request = (): ResearchModelRequest => {
    const snapshot = researchSnapshot(store, loop.id)
    return { user: { id: "fixture-user", role: "user", sessionID: contract.session, agent: "research", effort: "normal",
      time: { created: loop.created }, model: { providerID: "fixture", modelID: "fixture-model" } }, sessionID: contract.session,
    agent: { name: "research", mode: "primary", permission: [], options: {} },
    model: { id: "fixture-model", providerID: "fixture", name: "fixture", api: { id: "fixture-model", url: policy.endpoint_base_url, npm: "@ai-sdk/openai-compatible" },
      capabilities: { temperature: true, reasoning: true, attachment: false, toolcall: true,
        input: { text: true, audio: false, image: false, video: false, pdf: false },
        output: { text: true, audio: false, image: false, video: false, pdf: false }, interleaved: false },
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } }, limit: { context: 100000, output: 1024 },
      status: "active", options: {}, headers: {}, release_date: "fixture" },
    system: ["Synthetic fixture"], abort: new AbortController().signal, limits: { max_output_tokens: 100, timeout_ms: 1000 },
    context: snapshot.context, payload: snapshot.payload }
  }
  const identity = { store, id: loop.id, key: "fixture-call" }
  const prepare = (value = request()) => {
    const result = reserveResearchModelCall({ ...identity, request: value, policy, binding })
    if (result.kind !== "prepared") throw Error("Expected fresh fixture reservation")
    return result
  }
  return { ...identity, directory, filename, loop, contract, policy, request, prepare,
    live: () => ({ ...identity, policy, request: request() }) }
}
type Prepared = ReturnType<ReturnType<typeof fixture>["prepare"]>
function response(prepared: Prepared) {
  const decision = { schema: "drug-design-decision-v1" as const, loop_id: prepared.attempt.loop_id, state_revision: prepared.attempt.prepared_revision,
    action: "finish" as const, capability_id: null, plan_id: null, arguments: {}, evidence_refs: ["fixture:policy"],
    rationale: "Synthetic receipt; insufficient scientific evidence", alternatives: [], expected_observation: null,
    outcome: "insufficient_evidence" as const, missing_inputs: [] }
  return { text: JSON.stringify(decision), decision, validated: true as const,
    usage: { inputTokens: 80, outputTokens: 20, totalTokens: 100 }, finish_reason: "stop" as const,
    model: { providerID: "fixture", modelID: "fixture-model", response_model_id: "fixture-model" },
    prompt_sha256: prepared.attempt.prepared_prompt.sha256, prompt_hash_scope: "domain-and-decision-context" as const }
}

test("settled receipt survives reopening, commits original context once and needs no provider", async () => {
  const item = fixture()
  const prepared = item.prepare()
  const receipt = completeResearchModelCall({ ...item, result: response(prepared) })
  expect(receipt.expected_revision).toBe(prepared.attempt.prepared_revision + 2)
  const reopened = new ResearchLoopStore(item.filename)
  try {
    const result = await runResearchLiveDecision({ ...item.live(), store: reopened })
    expect(result.status).toBe("decision_committed")
    expect(result.invoked).toBe(false)
    expect(result.loop.outcome).toBe("insufficient_evidence")
    const rows = reopened.evidence(item.id, "decision")
    expect(rows).toHaveLength(1)
    expect(rows[0]!.payload).toMatchObject({ decision: { state_revision: prepared.attempt.prepared_revision },
      provenance: { kind: "model_response", receipt_ref: "model-receipt:fixture-call", request_digest: prepared.attempt.request_digest } })
    expect(await runResearchLiveDecision({ ...item.live(), store: reopened })).toMatchObject({ status: "already_committed", invoked: false })
    expect(reopened.evidence(item.id, "decision")).toHaveLength(1)
    expect(reopened.get(item.id).ledger.used.model_tokens).toBe(100)
  } finally { reopened.close() }
})

test("new key recovers the earlier completed receipt instead of discarding it", async () => {
  const item = fixture()
  completeResearchModelCall({ ...item, result: response(item.prepare()) })
  expect(await runResearchLiveDecision({ ...item.live(), key: "new-key" })).toMatchObject({ status: "decision_committed", invoked: false })
  expect(item.store.reservations(item.id)).toHaveLength(1)
})

test("unknown in-flight attempt remains reserved even when caller changes keys", async () => {
  const item = fixture()
  item.prepare()
  expect(await runResearchLiveDecision(item.live())).toMatchObject({ status: "in_flight_or_usage_unknown", invoked: false })
  expect(await runResearchLiveDecision({ ...item.live(), key: "new-key" })).toMatchObject({ status: "in_flight_or_usage_unknown", invoked: false })
  expect(item.store.reservations(item.id)).toHaveLength(1)
  expect(item.store.get(item.id).ledger.reserved.model_tokens).toBe(100100)
  expect(item.store.evidence(item.id, "decision")).toEqual([])
})

test.each(["transport", "missing_usage", "over_output", "wrong_provider", "invalid_response", "changed_state"] as const)(
  "%s receipt is never applied or retried", async kind => {
    const item = fixture()
    const prepared = item.prepare()
    const value = response(prepared)
    if (kind === "changed_state") item.store.record(item.id, "late-evidence", { changed: true })
    if (kind === "transport") failResearchModelCall(item)
    if (kind !== "transport") completeResearchModelCall({ ...item, result:
      kind === "missing_usage" ? { ...value, usage: { inputTokens: undefined, outputTokens: undefined, totalTokens: undefined } }
      : kind === "over_output" ? { ...value, usage: { inputTokens: 80, outputTokens: 101, totalTokens: 181 } }
      : kind === "wrong_provider" ? { ...value, model: { ...value.model, providerID: "unverified" } }
      : kind === "invalid_response" ? { ...value, validated: false, error: "Malformed JSON" } : value })
    expect(await runResearchLiveDecision({ ...item.live(), key: "another-key" })).toMatchObject({ status: "receipt_not_applicable", invoked: false })
    expect(item.store.evidence(item.id, "decision")).toEqual([])
    expect(item.store.reservations(item.id)).toHaveLength(1)
  })

test("post-receipt state changes and cancellation cannot rebase a prepared decision", async () => {
  for (const cancel of [false, true]) {
    const item = fixture()
    completeResearchModelCall({ ...item, result: response(item.prepare()) })
    if (cancel) item.store.finish(item.id, item.store.get(item.id).revision, "user_cancelled", ["fixture:policy"])
    if (!cancel) item.store.record(item.id, "post-receipt-evidence", { later: true })
    expect(await recoverResearchLiveDecision(item)).toMatchObject({ status: "receipt_not_applicable", invoked: false })
    expect(item.store.evidence(item.id, "decision")).toEqual([])
  }
})

test("response text must match the validated parsed proposal", async () => {
  const item = fixture()
  const value = response(item.prepare())
  completeResearchModelCall({ ...item, result: { ...value, text: JSON.stringify({ ...value.decision, rationale: "Different response" }) } })
  await expect(recoverResearchLiveDecision(item)).rejects.toThrow("actual response text")
  expect(item.store.evidence(item.id, "decision")).toEqual([])
})

test("stored context cannot introduce a capability absent from the current host", async () => {
  const item = fixture()
  const request = item.request()
  const prepared = item.prepare({ ...request, context: { ...request.context, capabilities: [{ id: "not-attached", version: "1",
    actions: ["inspect_evidence"], ready: true, schema: (await import("zod")).default.strictObject({}) }] } })
  completeResearchModelCall({ ...item, result: response(prepared) })
  await expect(recoverResearchLiveDecision(item)).rejects.toThrow("no longer attached")
  expect(item.store.evidence(item.id, "decision")).toEqual([])
})

test("two recovery callers cannot consume the same receipt twice", async () => {
  const item = fixture()
  completeResearchModelCall({ ...item, result: response(item.prepare()) })
  const reopened = new ResearchLoopStore(item.filename)
  try {
    const outcomes = await Promise.all([recoverResearchLiveDecision(item), recoverResearchLiveDecision({ ...item, store: reopened })])
    expect(outcomes.map(value => value.status).sort()).toEqual(["already_committed", "decision_committed"])
    expect(item.store.evidence(item.id, "decision")).toHaveLength(1)
  } finally { reopened.close() }
})

async function pending(item: ReturnType<typeof fixture>, committed = true) {
  const filename = path.join(item.directory, "evidence.json")
  const bytes = "Synthetic observation bytes"
  await Bun.write(filename, bytes)
  const artifact = await captureResearchArtifact({ path: filename, ref: "fixture-artifact", expectedSha256: createHash("sha256").update(bytes).digest("hex") })
  const observation = createResearchObservation({ origin: "replay_fixture", run: { id: "fixture-run", task_id: null,
    route: "library-first", release_id: hash, input_identity_digest: hash, started_at: null, status: "succeeded", phase: "fixture" },
    counts: { input_records: 20000, selected: 0, attempted: 0, execution_succeeded: 0, scientific_passed: null }, candidates: [], artifacts: [artifact],
    identity: { status: "unknown", evidence_refs: [], detail: "Not evaluated" }, execution_checks: [], scientific_checks: [],
    required_scientific_checks: ["identity"], failure: null, integrity_errors: [], limitations: ["Synthetic recovery test"] })
  item.store.observe(item.id, observation.id, observation)
  const prepared = item.prepare()
  const value = response(prepared)
  const decision = { ...value.decision, action: "inspect_evidence" as const, outcome: null, capability_id: "research.inspect-recorded-artifacts.v1",
    arguments: { observation_id: observation.id }, evidence_refs: [observation.id], expected_observation: "Verify saved file identity" }
  completeResearchModelCall({ ...item, result: { ...value, decision, text: JSON.stringify(decision) } })
  const loop = item.store.get(item.id)
  const guard = guardResearchAction(decision, { contract: loop.contract, context: prepared.request.context, observations: [observation],
    resources: loop.ledger.total, unsettled_compute: 0, completed_rounds: 0, decisions_used: 0, now: Date.now(),
    deadline: loop.created + loop.contract.limits.max_elapsed_ms, bound_task_ids: [], user_cancelled: false, approval_declined: false })
  expect(guard.accepted).toBe(true)
  if (!committed) return { observation, filename }
  // Reproduce the durable boundary immediately before the process crashes and
  // before commitResearchDecision starts its asynchronous local file read.
  item.store.db.transaction(() => {
    const saved = item.store.decide(item.id, loop.revision, { schema: "drug-design-recorded-proposal-v1", decision,
      provenance: { kind: "model_response", receipt_ref: "model-receipt:fixture-call", request_digest: prepared.attempt.request_digest } })
    item.store.record(item.id, `guard-${saved.decision_id}`, { schema: "drug-design-action-guard-v1", ...guard })
    item.store.transition(item.id, item.store.get(item.id).revision, "EVALUATING", { reason: "fixture-crash-before-file-read" })
  }).immediate()
  return { observation, filename }
}

test("prepared inspection context restores its exact Zod capability and executes the local read", async () => {
  const item = fixture()
  const evidence = await pending(item, false)
  const result = await recoverResearchLiveDecision(item)
  expect(result.status).toBe("decision_committed")
  expect(result.loop.state).toBe("DECIDING")
  expect(item.store.evidence(item.id, "host").find(row => row.key === `inspection-${evidence.observation.id}`)?.payload)
    .toMatchObject({ passed: true, scientific_evaluation_performed: false })
  expect(researchSnapshot(item.store, item.id).context.capabilities).toEqual([])
  expect(await recoverResearchLiveDecision(item)).toMatchObject({ status: "already_committed", invoked: false })
})

test.each([false, true])("pending local inspection resumes without model; file changed=%s", async changed => {
  const item = fixture()
  const evidence = await pending(item)
  if (changed) await Bun.write(evidence.filename, "changed after original observation")
  const reopened = new ResearchLoopStore(item.filename)
  try {
    const result = await runResearchLiveDecision({ ...item.live(), store: reopened, key: "new-key" })
    expect(result).toMatchObject({ status: "action_recovered", invoked: false, resumed: true })
    expect(result.loop.state).toBe(changed ? "BLOCKED" : "DECIDING")
    expect(reopened.evidence(item.id, "host").find(row => row.key === `inspection-${evidence.observation.id}`)?.payload)
      .toMatchObject({ passed: !changed, scientific_evaluation_performed: false })
    expect(await runResearchLiveDecision({ ...item.live(), store: reopened })).toMatchObject({ status: "already_committed", invoked: false })
    expect(reopened.evidence(item.id, "decision")).toHaveLength(1)
    expect(reopened.reservations(item.id)).toHaveLength(1)
  } finally { reopened.close() }
})

test("read-only recovery does nothing without an attempt and fresh aborted run never reserves", async () => {
  const item = fixture()
  expect(await recoverResearchLiveDecision(item)).toMatchObject({ status: "no_attempt", invoked: false })
  await expect(runResearchLiveDecision({ ...item.live(), request: { ...item.request(), abort: AbortSignal.abort() } })).rejects.toThrow()
  expect(item.store.reservations(item.id)).toEqual([])
})

test.each(["decision", "deadline"] as const)("host stops at exhausted %s budget without another model or decision slot", async kind => {
  const item = fixture({ max_decisions: 1 }, kind === "deadline" ? Date.now() - 60001 : Date.now())
  if (kind === "decision") item.store.decide(item.id, item.store.get(item.id).revision, { scope: "Synthetic already-consumed decision slot" })
  const before = item.store.get(item.id)
  const result = await runResearchLiveDecision(item.live())
  expect(result).toMatchObject({ status: "budget_exhausted", invoked: false, loop: { state: "FINISHED", outcome: "budget_exhausted" } })
  expect(result.loop.ledger).toEqual(before.ledger)
  expect(item.store.evidence(item.id, "decision")).toHaveLength(kind === "decision" ? 1 : 0)
  expect(item.store.reservations(item.id)).toEqual([])
  expect(item.store.evidence(item.id, "host").find(row => row.key === "host-budget-stop")?.payload)
    .toMatchObject({ based_on_revision: before.revision, deadline: item.loop.created + item.contract.limits.max_elapsed_ms, ledger: before.ledger })
})

function unbounded() {
  const item = fixture({ resources: { ...emptyResources(), model_tokens: 300000, model_cost: null } })
  item.store.record(item.id, "fixture:authorization", { content: {
    schema: "research-model-cost-authorization-v1", origin: "explicit-user-instruction", scope: "model-fees-only",
    model_cost_limit: null, source_ref: "synthetic-test-only", statement: "Test explicit uncapped fees",
    recorded_at: new Date().toISOString(),
  } })
  item.policy.evidence_refs.push("fixture:authorization")
  item.policy.pricing = { basis: "user-authorized-unbounded", currency: null, input_per_million: null,
    output_per_million: null, authorization_ref: "fixture:authorization", valid_until: new Date(Date.now() + 86400000).toISOString() }
  return item
}

test("explicitly uncapped costs remain unknown while real token receipts recover and commit once", async () => {
  const item = unbounded()
  const prepared = item.prepare()
  const receipt = completeResearchModelCall({ ...item, result: response(prepared) })
  expect(receipt.receipt?.accounting).toMatchObject({ state: "usage-settled-cost-unknown", model_cost_upper_bound: null, currency: null })
  const reopened = new ResearchLoopStore(item.filename)
  try {
    expect(reopened.get(item.id).ledger.model_cost_accounting).toMatchObject({ limit: "unbounded", total_cost: null,
      unpriced_calls: [item.key] })
    expect(reopened.get(item.id).ledger.used.model_tokens).toBe(100)
    expect(await runResearchLiveDecision({ ...item.live(), store: reopened })).toMatchObject({ status: "decision_committed", invoked: false })
    expect(await runResearchLiveDecision({ ...item.live(), store: reopened })).toMatchObject({ status: "already_committed", invoked: false })
    expect(reopened.reservations(item.id)).toHaveLength(1)
  } finally { reopened.close() }
})

test("uncapped cost never permits a second request with missing usage", async () => {
  const item = unbounded()
  item.prepare()
  failResearchModelCall(item)
  expect(await runResearchLiveDecision({ ...item.live(), key: "different-key" })).toMatchObject({ status: "receipt_not_applicable", invoked: false })
  expect(item.store.reservations(item.id)).toHaveLength(1)
  expect(item.store.get(item.id).ledger.reserved.model_tokens).toBe(100100)
})

test("uncapped prices cannot bypass a capped contract or missing user authorization", () => {
  const item = fixture()
  item.policy.pricing = { basis: "user-authorized-unbounded", currency: null, input_per_million: null,
    output_per_million: null, authorization_ref: "fixture:policy", valid_until: new Date(Date.now() + 86400000).toISOString() }
  expect(() => item.prepare()).toThrow("uncapped contract")
  const unpriced = unbounded()
  if (unpriced.policy.pricing.basis !== "user-authorized-unbounded") throw Error("Expected unbounded test policy")
  unpriced.policy.pricing.authorization_ref = "fixture:policy"
  expect(() => unpriced.prepare()).toThrow()
  expect(unpriced.store.reservations(unpriced.id)).toHaveLength(0)
})

test("uncapped costs preserve token and scientific-resource bounds", () => {
  const item = unbounded()
  const request = item.request()
  request.limits.max_output_tokens = 1025
  expect(() => item.prepare(request)).toThrow("output")
  const current = item.store.get(item.id)
  expect(() => item.store.reserve(item.id, current.revision, { key: "too-many-tokens", kind: "model", round: 0,
    candidate_slots: 0, plan_digest: hash, source_release: hash, resources: { ...emptyResources(), model_tokens: 300001 } })).toThrow("model_tokens")
  expect(() => item.store.reserve(item.id, current.revision, { key: "gpu", kind: "compute", round: 1,
    candidate_slots: 1, plan_digest: hash, source_release: hash, resources: { ...emptyResources(), gpu_hours: 0.1 } })).toThrow("gpu_hours")
})
