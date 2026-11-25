import { afterAll, expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, type ResearchContract } from "./research-loop-contract"
import { createResearchObservation, captureResearchArtifact, importLibraryFirstMigrationObservation, type ResearchObservationInput } from "./research-observations"
import { researchSnapshot, applyResearchReplayProposal } from "./research-loop"
import { guardResearchAction, type ResearchGuardState } from "./research-action-guard"

const scratch = await mkdtemp(path.join(os.tmpdir(), "research-control-"))
const stores: ResearchLoopStore[] = []
afterAll(async () => { stores.forEach(store => store.close()); await rm(scratch, { recursive: true, force: true }) })
const bytes = JSON.stringify({ kind: "synthetic-replay-check", not_a_scientific_result: true })
const hash = createHash("sha256").update(bytes).digest("hex")
const filename = path.join(scratch, "fixture-science-manifest.json")
await Bun.write(filename, bytes)

function contract(): ResearchContract {
  return { schema: "drug-design-research-contract-v1", directory: scratch, project: "offline-fixture", session: "fixture",
    objective: "Verify host behavior, not molecule quality", mode: "offline-replay", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: hash, mapping_sha256: hash },
    inputs: { library: "fixture", library_sha256: hash, subset_sha256: hash, source_release: hash, science_config_sha256: hash },
    quality: { manifest_sha256: hash, required_checks: ["fixture_geometry"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { ...emptyResources(), model_tokens: 1000, model_cost: 1 },
      max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
      max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 12, max_elapsed_ms: 60000 },
    allowed_adaptations: ["inspect", "stop"] }
}
async function observation(): Promise<ResearchObservationInput> {
  return { origin: "replay_fixture", run: { id: "synthetic-run", task_id: null, route: "library-first",
    release_id: hash, input_identity_digest: hash, started_at: null, status: "succeeded", phase: "fixture-evaluation" },
    counts: { input_records: 20000, selected: 1, attempted: 1, execution_succeeded: 1, scientific_passed: 1 },
    candidates: [{ id: "fixture-molecule", smiles: "C=CC(=O)N", execution_status: "succeeded", scientific_status: "pass", scores: [] }],
    artifacts: [await captureResearchArtifact({ path: filename, ref: "fixture-manifest", expectedSha256: hash })],
    identity: { status: "pass", evidence_refs: ["fixture-manifest"], detail: "Synthetic identity only" },
    execution_checks: [{ id: "fixture_engine", status: "pass", evaluator: "fixture-v1", evidence_refs: ["fixture-manifest"], detail: "Fixture" }],
    scientific_checks: [{ id: "fixture_geometry", status: "pass", evaluator: "fixture-v1", evidence_refs: ["fixture-manifest"], detail: "Synthetic check; not scientific evidence" }],
    required_scientific_checks: ["fixture_geometry"], failure: null, integrity_errors: [], limitations: ["Synthetic replay"] }
}
async function setup(value?: ResearchObservationInput) {
  const db = path.join(scratch, `loop-${stores.length}.sqlite`)
  const store = new ResearchLoopStore(db)
  stores.push(store)
  const loop = store.create(contract())
  const observed = createResearchObservation(value ?? await observation())
  store.observe(loop.id, observed.id, observed)
  return { store, id: loop.id, observed, db }
}
function decision(store: ResearchLoopStore, id: string, outcome = "objective_met") {
  const snapshot = researchSnapshot(store, id)
  return { schema: "drug-design-decision-v1", loop_id: id, state_revision: snapshot.loop.revision,
    action: "finish", capability_id: null, plan_id: null, arguments: {}, evidence_refs: [snapshot.observations[0]!.id],
    rationale: "Scripted proposal for testing host guards", alternatives: [], expected_observation: null,
    outcome, missing_inputs: [] }
}
function state(store: ResearchLoopStore, id: string): ResearchGuardState {
  const snapshot = researchSnapshot(store, id)
  return { contract: snapshot.loop.contract, context: snapshot.context, observations: snapshot.observations,
    resources: snapshot.loop.ledger.total, unsettled_compute: 0, completed_rounds: 0, decisions_used: 0,
    now: Date.now(), deadline: snapshot.loop.created + 60000, bound_task_ids: [], user_cancelled: false, approval_declined: false }
}

test("scripted stopping remains replay-only and survives database reopen", async () => {
  const item = await setup()
  const result = await applyResearchReplayProposal({ ...item, value: decision(item.store, item.id, "insufficient_evidence"), case_id: "normal" })
  expect(result.accepted).toBe(true)
  expect(result.new_research_result).toBe(false)
  expect(result.loop.outcome).toBe("insufficient_evidence")
  const reopened = new ResearchLoopStore(item.db)
  stores.push(reopened)
  expect(researchSnapshot(reopened, item.id).loop).toEqual(result.loop)
  expect(researchSnapshot(reopened, item.id).context.allowed_actions).toEqual([])
  expect(reopened.evidence(item.id, "decision")[0]!.payload).toMatchObject({ provenance: { kind: "scripted_replay" } })
})

test("real historical 5/5 results cannot be relabelled scientific success", async () => {
  const root = path.resolve(import.meta.dir, "../../../../..")
  const observed = await importLibraryFirstMigrationObservation({ reportPath: path.join(root, "reports/layout-migration-20261012/gpu-validation.json"),
    artifactsDirectory: path.join(root, "reports/layout-migration-20261012/gpu-final") })
  const item = await setup()
  const loop = item.store.create({ ...contract(), quality: { ...contract().quality, manifest_sha256: null } })
  item.store.observe(loop.id, observed.id, observed)
  const result = await applyResearchReplayProposal({ store: item.store, id: loop.id, value: decision(item.store, loop.id), case_id: "real-history" })
  expect(result.accepted).toBe(false)
  expect(result.loop.state).toBe("BLOCKED")
  expect(result.loop.outcome).toBeNull()
  expect(observed.counts.scientific_passed).toBeNull()
})

test("zero candidates and structural failure can stop; engine failure cannot imply no viable molecule", async () => {
  const zero = await observation()
  zero.candidates = []
  zero.counts = { input_records: 20000, selected: 0, attempted: 0, execution_succeeded: 0, scientific_passed: 0 }
  zero.scientific_checks = []
  const empty = await setup(zero)
  expect(guardResearchAction(decision(empty.store, empty.id, "no_viable_candidate"), state(empty.store, empty.id)).accepted).toBe(true)
  const structure = await observation()
  structure.scientific_checks[0]!.status = "fail"
  structure.candidates[0]!.scientific_status = "fail"
  structure.counts.scientific_passed = 0
  const failed = await setup(structure)
  expect(guardResearchAction(decision(failed.store, failed.id, "objective_met"), state(failed.store, failed.id)).accepted).toBe(false)
  expect(guardResearchAction(decision(failed.store, failed.id, "no_viable_candidate"), state(failed.store, failed.id)).accepted).toBe(true)
  const engine = await observation()
  engine.run.status = "failed"
  engine.failure = { category: "environment", detail: "Synthetic engine failure", evidence_refs: ["fixture-manifest"] }
  const broken = await setup(engine)
  expect(guardResearchAction(decision(broken.store, broken.id, "no_viable_candidate"), state(broken.store, broken.id)).accepted).toBe(false)
  expect(guardResearchAction(decision(broken.store, broken.id, "insufficient_evidence"), state(broken.store, broken.id)).accepted).toBe(true)
})

test("cancellation, permission refusal, budget and native provenance require host evidence", async () => {
  const item = await setup()
  const context = state(item.store, item.id)
  for (const outcome of ["user_cancelled", "approval_declined", "budget_exhausted"])
    expect(guardResearchAction(decision(item.store, item.id, outcome), context).accepted).toBe(false)
  expect(guardResearchAction(decision(item.store, item.id, "budget_exhausted"), { ...context, now: context.deadline }).accepted).toBe(true)
  expect(guardResearchAction(decision(item.store, item.id), { ...context, unsettled_compute: 1 }).accepted).toBe(false)
  expect(guardResearchAction(decision(item.store, item.id), { ...context, contract: { ...context.contract, mode: "native-compute" } }).accepted).toBe(false)
})

test("a refusal finish exception cannot hide unrelated unsettled compute or omit its confirmed receipt", async () => {
  const item = await setup()
  // SYNTHETIC guard-input fixture only; native-controller tests establish the
  // real persistent provenance before this host flag can be set.
  item.store.record(item.id, "synthetic-confirmed-refusal", { synthetic_fixture: true })
  const context = { ...state(item.store, item.id), approval_declined: true, approval_declined_unsubmitted: 1,
    approval_declined_refs: ["synthetic-confirmed-refusal"], unsettled_compute: 1 }
  const proposal = { ...decision(item.store, item.id, "approval_declined"), evidence_refs: ["synthetic-confirmed-refusal"] }
  expect(guardResearchAction(proposal, context).accepted).toBe(true)
  expect(guardResearchAction(proposal, { ...context, unsettled_compute: 2 }).accepted).toBe(false)
  expect(guardResearchAction(proposal, { ...context, approval_declined_unsubmitted: 0 }).accepted).toBe(false)
  expect(guardResearchAction({ ...proposal, evidence_refs: [item.observed.id] }, context).accepted).toBe(false)
  expect(guardResearchAction({ ...proposal, outcome: "insufficient_evidence" }, context).accepted).toBe(false)
})

test("inspection really rehashes files; it cannot serve as a scientific evaluation or repeat unboundedly", async () => {
  const item = await setup()
  const proposal = { ...decision(item.store, item.id), action: "inspect_evidence", outcome: null,
    capability_id: "research.inspect-recorded-artifacts.v1", arguments: { observation_id: item.observed.id },
    expected_observation: "Verify existing artifact bytes only" }
  const result = await applyResearchReplayProposal({ ...item, value: proposal, case_id: "inspect" })
  expect(result.loop.state).toBe("DECIDING")
  expect(researchSnapshot(item.store, item.id).context.capabilities).toEqual([])
  expect(item.store.evidence(item.id, "host").find(row => row.key.startsWith("inspection-"))!.payload)
    .toMatchObject({ passed: true, scientific_evaluation_performed: false })
  await expect(applyResearchReplayProposal({ ...item, value: proposal, case_id: "duplicate" })).rejects.toThrow("stale")
  expect(item.store.evidence(item.id, "decision")).toHaveLength(1)
})

test("unregistered next-batch requests are refused before recording or changing state", async () => {
  const item = await setup()
  const proposal = { ...decision(item.store, item.id), action: "prepare_action", outcome: null,
    capability_id: "next_frozen_batch", expected_observation: "New candidates" }
  await expect(applyResearchReplayProposal({ ...item, value: proposal, case_id: "unsupported" })).rejects.toThrow("not in allowed_actions")
  expect(item.store.evidence(item.id, "decision")).toHaveLength(0)
  expect(item.store.reservations(item.id)).toEqual([])
})

test("a final host budget stop needs no additional decision slot", async () => {
  const item = await setup()
  const limits = { ...contract().limits, max_decisions: 1 }
  const loop = item.store.create({ ...contract(), limits })
  item.store.observe(loop.id, item.observed.id, item.observed)
  item.store.decide(loop.id, item.store.get(loop.id).revision, { origin: "scripted_replay", action: "inspect" })
  const result = await applyResearchReplayProposal({ store: item.store, id: loop.id, value: {}, case_id: "budget-stop" })
  expect(result.loop.outcome).toBe("budget_exhausted")
  expect(item.store.evidence(loop.id, "decision")).toHaveLength(1)
  expect(item.store.evidence(loop.id, "host").some(row => row.key === "host-budget-stop")).toBe(true)
})

test("inspection cannot verify a file whose original hash was missing", async () => {
  const input = await observation()
  input.artifacts[0] = { ...input.artifacts[0]!, sha256: null, expected_sha256: null, size_bytes: null, status: "missing" }
  const item = await setup(input)
  const proposal = { ...decision(item.store, item.id), action: "inspect_evidence", outcome: null,
    capability_id: "research.inspect-recorded-artifacts.v1", arguments: { observation_id: item.observed.id }, expected_observation: "Check known artifact" }
  const result = await applyResearchReplayProposal({ ...item, value: proposal, case_id: "unanchored-file" })
  expect(result.loop.state).toBe("BLOCKED")
  expect(item.store.evidence(item.id, "host").find(row => row.key.startsWith("inspection-"))!.payload).toMatchObject({ passed: false })
})

test("model evidence stays immutable while subsequent prompts omit embedded prior prompts", async () => {
  const item = await setup()
  const prior = { schema: "research-model-attempt-v1", request_digest: hash,
    prepared_prompt: { system: "previous-system-only", user: "previous-full-observations-only", sha256: hash } }
  item.store.record(item.id, "model-attempt:fixture", prior)
  item.store.record(item.id, "model-receipt:fixture", { schema: "research-model-receipt-v1", request_digest: hash,
    status: "completed", accounting: { state: "settled", usage: { total_tokens: 128 } },
    result: { text: "previous-raw-response-only", prompt_sha256: hash } })
  const snapshot = researchSnapshot(item.store, item.id)
  expect(snapshot.prompt.user).not.toContain("previous-system-only")
  expect(snapshot.prompt.user).not.toContain("previous-full-observations-only")
  expect(snapshot.prompt.user).not.toContain("previous-raw-response-only")
  expect(snapshot.prompt.user).toContain('"total_tokens":128')
  expect(snapshot.context.evidence_refs).toContain("model-attempt:fixture")
  expect(item.store.evidence(item.id, "host").find(row => row.key === "model-attempt:fixture")!.payload).toEqual(prior)
})
