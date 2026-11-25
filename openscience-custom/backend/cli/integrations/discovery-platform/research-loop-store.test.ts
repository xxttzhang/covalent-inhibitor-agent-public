import { afterEach, describe, expect, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, researchContract, researchDigest, type ResearchContract, type ResearchResources } from "./research-loop-contract"

// Synthetic contracts exercise persistence invariants. These hashes are test
// fixture identities, not claims of scientific validation or production inputs.
const hash = researchDigest("synthetic-store-test-fixture")
const contract = (): ResearchContract => ({
  schema: "drug-design-research-contract-v1", directory: "/test/project", project: "test", session: "session-test",
  objective: "Exercise durable research-loop invariants", mode: "native-compute", route: "library-first",
  target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
    author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: hash, mapping_sha256: hash },
  inputs: { library: "synthetic-library", library_sha256: hash, subset_sha256: hash, source_release: hash, science_config_sha256: hash },
  quality: { manifest_sha256: hash, required_checks: ["identity", "geometry"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
  limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 4, compute_points: 10, model_tokens: 10000, model_cost: 1 },
    max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
    max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 5, max_elapsed_ms: 60000 },
  allowed_adaptations: ["inspect", "evaluate", "next_frozen_batch", "stop"],
})
const compute = (key = "round-one", round = 1, resources: Partial<ResearchResources> = {}) => ({
  key, kind: "compute", round, candidate_slots: 5, plan_digest: researchDigest(key), source_release: hash,
  resources: { ...emptyResources(), gpu_hours: 0.25, cpu_core_hours: 1, compute_points: 2, ...resources },
})
const model = (key = "model-one", resources: Partial<ResearchResources> = {}) => ({
  key, kind: "model", round: 0, candidate_slots: 0, plan_digest: researchDigest(key), source_release: hash,
  resources: { ...emptyResources(), model_tokens: 2000, model_cost: 0.1, ...resources },
})
const evaluation = (key = "evaluation-one", cpu = 0.1) => ({
  key, kind: "evaluation", round: 0, candidate_slots: 0, plan_digest: researchDigest(key), source_release: hash,
  resources: { ...emptyResources(), cpu_core_hours: cpu },
})
const fixtures: { directory: string; stores: ResearchLoopStore[] }[] = []
function fixture(input: ResearchContract = contract()) {
  const directory = mkdtempSync(path.join(os.tmpdir(), "research-loop-store-"))
  const filename = path.join(directory, "research.sqlite")
  const store = new ResearchLoopStore(filename)
  const entry = { directory, stores: [store] }
  fixtures.push(entry)
  const loop = store.create(input)
  const connect = () => {
    const next = new ResearchLoopStore(filename)
    entry.stores.push(next)
    return next
  }
  const close = () => {
    store.close()
    entry.stores.splice(entry.stores.indexOf(store), 1)
  }
  return { store, loop, filename, directory, connect, close }
}
afterEach(() => {
  for (const fixture of fixtures.splice(0)) {
    for (const store of fixture.stores) store.close()
    rmSync(fixture.directory, { recursive: true, force: true })
  }
})

describe("frozen research contract", () => {
  test("keeps offline missing scientific bindings explicit and refuses native execution without them", () => {
    const input = contract()
    input.mode = "offline-replay"
    input.target.receptor_sha256 = null
    input.target.mapping_sha256 = null
    input.inputs.library_sha256 = null
    input.inputs.subset_sha256 = null
    input.inputs.science_config_sha256 = null
    input.quality.manifest_sha256 = null
    const { store, loop } = fixture(input)
    expect(store.get(loop.id).contract).toEqual(input)
    expect(researchContract.safeParse({ ...input, mode: "native-compute" }).success).toBe(false)
    expect(researchContract.safeParse({ ...input, inputs: { ...input.inputs, source_release: null } }).success).toBe(false)
    expect(() => store.reserve(loop.id, loop.revision, compute())).toThrow("Offline replay")
  })

  test("enforces pilot caps and immutable source release", () => {
    const input = contract()
    expect(researchContract.safeParse({ ...input, limits: { ...input.limits, max_rounds: 3 } }).success).toBe(false)
    expect(researchContract.safeParse({ ...input, limits: { ...input.limits, max_candidates_per_round: 6 } }).success).toBe(false)
    expect(researchContract.safeParse({ ...input, limits: { ...input.limits, max_rounds: 1 } }).success).toBe(false)
    expect(researchContract.safeParse({ ...input, limits: { ...input.limits, resources: { ...input.limits.resources, gpu_hours: 0.6 } } }).success).toBe(false)
    const { store, loop } = fixture()
    expect(() => store.reserve(loop.id, 0, { ...compute(), source_release: researchDigest("different") })).toThrow("source release")
    expect(store.reservations(loop.id)).toHaveLength(0)
  })
})

describe("durable evidence and recovery", () => {
  test("reopens SQLite with exact contract, evidence, ledger and event history", () => {
    const { store, loop, connect, close } = fixture()
    const observed = store.observe(loop.id, "run:one", { source: "run:historical", checks: { geometry: "unknown" } })
    const host = store.record(loop.id, "receipt:model", { provider: "fixture", usage: null })
    store.reserve(loop.id, host.revision, model())
    const expected = { loop: store.get(loop.id), evidence: store.evidence(loop.id), events: store.events(loop.id) }
    close()
    const recovered = connect()
    expect(recovered.get(loop.id)).toEqual(expected.loop)
    expect(recovered.evidence(loop.id)).toEqual(expected.evidence)
    expect(recovered.evidence(loop.id, "observation")).toHaveLength(1)
    expect(recovered.evidence(loop.id, "host")).toHaveLength(1)
    expect(recovered.events(loop.id)).toEqual(expected.events)
    expect(recovered.list("/test/project").map(item => item.id)).toEqual([loop.id])
    expect(recovered.list("/other")).toEqual([])
    expect(observed.state).toBe("DECIDING")
    expect(recovered.get(loop.id).ledger.unsettled).toEqual(["model-one"])
  })

  test("observation delivery is idempotent and cannot reuse another evidence identity", () => {
    const { store, loop, connect } = fixture()
    const value = { candidates: ["a"], failures: 2, validation: "partial" }
    const first = store.observe(loop.id, "run:one", value)
    const next = connect().observe(loop.id, "run:one", { failures: 2, validation: "partial", candidates: ["a"] })
    expect(next.revision).toBe(first.revision)
    expect(store.events(loop.id)).toHaveLength(2)
    expect(() => store.observe(loop.id, "run:one", { ...value, failures: 0 })).toThrow("identity reused")
    expect(() => store.record(loop.id, "run:one", value)).toThrow("identity reused")
    expect(() => store.observe(loop.id, " ", value)).toThrow("identity")
    expect(store.get(loop.id).revision).toBe(first.revision)
  })

  test("host receipts preserve execution state and remain distinct from scientific observations", () => {
    const { store, loop } = fixture()
    const running = store.transition(loop.id, loop.revision, "RUNNING", { task: "existing" })
    const first = store.record(loop.id, "plan:one", { plan_digest: hash, state: "DRAFT" })
    expect(first.state).toBe("RUNNING")
    expect(first.revision).toBe(running.revision + 1)
    expect(store.record(loop.id, "plan:one", { plan_digest: hash, state: "DRAFT" }).revision).toBe(first.revision)
    expect(() => store.record(loop.id, "plan:one", { plan_digest: hash, state: "APPROVED" })).toThrow("identity reused")
    expect(() => store.observe(loop.id, "plan:one", { plan_digest: hash, state: "DRAFT" })).toThrow("identity reused")
    expect(store.evidence(loop.id, "observation")).toEqual([])
  })

  test("optimistic revisions reject a second controller's stale decision and transitions", () => {
    const { store, loop, connect } = fixture()
    const second = connect()
    const first = store.decide(loop.id, loop.revision, { action: "inspect_evidence", rationale: "fixture" })
    expect(first.loop.revision).toBe(1)
    expect(() => second.decide(loop.id, loop.revision, { action: "finish" })).toThrow("Stale")
    expect(() => second.transition(loop.id, loop.revision, "PREPARING", {})).toThrow("Stale")
    expect(store.evidence(loop.id, "decision")).toHaveLength(1)
  })
})

describe("cumulative resource accounting", () => {
  test("unused reservations remain charged across recovery and actual usage accumulates across rounds", () => {
    const { store, loop, connect } = fixture()
    store.reserve(loop.id, 0, compute())
    expect(connect().get(loop.id).ledger.reserved.gpu_hours).toBe(0.25)
    expect(() => store.reserve(loop.id, store.get(loop.id).revision, compute("round-two", 2))).toThrow("unsettled")
    const actual = { ...emptyResources(), gpu_hours: 0.1, cpu_core_hours: 0.4, compute_points: 1 }
    const first = store.settle(loop.id, "round-one", actual, "receipt:round-one")
    expect(first.ledger.used).toEqual(actual)
    expect(first.ledger.reserved).toEqual(emptyResources())
    store.reserve(loop.id, first.revision, compute("round-two", 2))
    expect(store.get(loop.id).ledger.total.gpu_hours).toBeCloseTo(0.35)
    const final = store.settle(loop.id, "round-two", actual, "receipt:round-two")
    expect(final.ledger.used.gpu_hours).toBeCloseTo(0.2)
    expect(final.ledger.used.compute_points).toBe(2)
    expect(final.ledger.unsettled).toEqual([])
    expect(() => store.reserve(loop.id, final.revision, { ...compute("round-three", 2) })).toThrow("round")
  })

  test("reservation identity is immutable and replay never increments budgets or revision", () => {
    const { store, loop, connect } = fixture()
    const first = store.reserve(loop.id, 0, model())
    const revision = store.get(loop.id).revision
    expect(connect().reserve(loop.id, 0, model())).toEqual(first)
    expect(store.get(loop.id).revision).toBe(revision)
    expect(store.get(loop.id).ledger.total.model_tokens).toBe(2000)
    expect(() => store.reserve(loop.id, revision, model("model-one", { model_tokens: 3000 }))).toThrow("changed plan")
  })

  test("unknown model consumption blocks another call and cannot be treated as zero", () => {
    const { store, loop, connect } = fixture()
    store.reserve(loop.id, 0, model())
    expect(() => connect().reserve(loop.id, store.get(loop.id).revision, model("model-two"))).toThrow("unsettled")
    const settled = store.settle(loop.id, "model-one", { ...emptyResources(), model_tokens: 1500, model_cost: 0.05 }, "provider:receipt")
    store.reserve(loop.id, settled.revision, model("model-two"))
    expect(store.get(loop.id).ledger.total.model_tokens).toBe(3500)
    expect(store.get(loop.id).ledger.total.model_cost).toBeCloseTo(0.15)
  })

  test("all resource dimensions have cumulative ceilings", () => {
    for (const [field, value] of [["cpu_core_hours", 5], ["compute_points", 11]] as const) {
      const { store, loop } = fixture()
      expect(() => store.reserve(loop.id, 0, compute("round-one", 1, { [field]: value }))).toThrow("Cumulative research budget")
      expect(store.get(loop.id).revision).toBe(0)
    }
    for (const [field, value] of [["model_tokens", 10001], ["model_cost", 1.01]] as const) {
      const { store, loop } = fixture()
      expect(() => store.reserve(loop.id, 0, model("model-one", { [field]: value }))).toThrow("Cumulative research budget")
      expect(store.get(loop.id).revision).toBe(0)
    }
    const { store, loop } = fixture()
    expect(() => store.reserve(loop.id, 0, compute("round-one", 1, { gpu_hours: 0.251 }))).toThrow("Per-round GPU")
  })

  test("enforces per-round and total candidate counts even when resource use is low", () => {
    const input = contract()
    input.limits.max_total_candidates = 6
    const { store, loop } = fixture(input)
    expect(() => store.reserve(loop.id, 0, { ...compute(), candidate_slots: 6 })).toThrow()
    store.reserve(loop.id, 0, compute())
    const settled = store.settle(loop.id, "round-one", emptyResources(), "cancelled-before-compute:one")
    expect(() => store.reserve(loop.id, settled.revision, compute("round-two", 2))).toThrow("candidate limit")
    expect(store.reservations(loop.id)).toHaveLength(1)
  })

  test("model call attempts and stored decisions each obey the decision cap", () => {
    const input = contract()
    input.limits.max_decisions = 1
    const { store, loop } = fixture(input)
    store.reserve(loop.id, 0, model())
    const settled = store.settle(loop.id, "model-one", emptyResources(), "failed-before-charge:one")
    expect(() => store.reserve(loop.id, settled.revision, model("model-two"))).toThrow("decision budget")
    const first = store.decide(loop.id, settled.revision, { action: "blocked" })
    expect(() => store.decide(loop.id, first.loop.revision, { action: "blocked" })).toThrow("decision budget")
  })

  test("settlement is immutable, requires a receipt, and records overages without hiding actual cost", () => {
    const { store, loop } = fixture()
    store.reserve(loop.id, 0, model())
    expect(() => store.settle(loop.id, "model-one", emptyResources(), " ")).toThrow("verified receipt")
    const actual = { ...emptyResources(), model_tokens: 2500, model_cost: 0.15 }
    const blocked = store.settle(loop.id, "model-one", actual, "provider:overage")
    expect(blocked.state).toBe("BLOCKED")
    expect(blocked.ledger.used.model_tokens).toBe(2500)
    expect(store.settle(loop.id, "model-one", actual, "provider:overage").revision).toBe(blocked.revision)
    expect(() => store.settle(loop.id, "model-one", emptyResources(), "provider:overage")).toThrow("immutable")
    expect(() => store.settle(loop.id, "model-one", actual, "provider:replacement")).toThrow("immutable")
    // New evidence or a state transition cannot erase a budget breach.
    const observed = store.observe(loop.id, "late:observation", { finished: true })
    expect(() => store.reserve(loop.id, observed.revision, model("model-two"))).toThrow("breach")
  })

  test("wall-clock budget prevents new work but permits truthful terminal accounting", () => {
    const { store, loop } = fixture()
    const expired = loop.created + loop.contract.limits.max_elapsed_ms
    expect(() => store.reserve(loop.id, 0, model(), expired)).toThrow("wall-clock")
    const recorded = store.record(loop.id, "budget:expired", { max_elapsed_ms: loop.contract.limits.max_elapsed_ms }, expired)
    const finished = store.finish(loop.id, recorded.revision, "budget_exhausted", ["budget:expired"], expired)
    expect(finished.outcome).toBe("budget_exhausted")
  })
})

describe("local scientific evaluation accounting", () => {
  test("evaluation keeps the unknown native bill charged, survives recovery and does not consume another candidate round", () => {
    const { store, loop, connect } = fixture()
    store.reserve(loop.id, 0, compute())
    store.reserve(loop.id, store.get(loop.id).revision, evaluation())
    const recovered = connect()
    const before = recovered.get(loop.id)
    expect(before.ledger.total.cpu_core_hours).toBeCloseTo(1.1)
    expect(before.ledger.unsettled).toEqual(["round-one", "evaluation-one"])
    recovered.reserve(loop.id, 0, evaluation())
    expect(recovered.get(loop.id).revision).toBe(before.revision)
    const evaluated = recovered.settle(loop.id, "evaluation-one", { ...emptyResources(), cpu_core_hours: 0.04 }, "SYNTHETIC-evaluator-receipt")
    expect(evaluated.ledger.reserved.cpu_core_hours).toBe(1)
    expect(evaluated.ledger.used.cpu_core_hours).toBe(0.04)
    expect(evaluated.ledger.unsettled).toEqual(["round-one"])
    recovered.settle(loop.id, "round-one", { ...emptyResources(), gpu_hours: 0.1, cpu_core_hours: 0.4 }, "SYNTHETIC-platform-bill")
    recovered.reserve(loop.id, recovered.get(loop.id).revision, compute("round-two", 2))
    expect(recovered.reservations(loop.id).filter(row => row.payload.kind === "compute").map(row => row.payload.round)).toEqual([1, 2])
    expect(recovered.get(loop.id).ledger.total.cpu_core_hours).toBeCloseTo(1.44)
  })

  test("uncertain evaluation blocks another evaluation, model or native round until actual usage is settled", () => {
    const { store, loop, connect } = fixture()
    store.reserve(loop.id, 0, evaluation())
    const recovered = connect()
    for (const item of [evaluation("evaluation-two"), model(), compute()])
      expect(() => recovered.reserve(loop.id, recovered.get(loop.id).revision, item)).toThrow("evaluation is running or its resource usage is unsettled")
    expect(recovered.reservations(loop.id)).toHaveLength(1)
    recovered.settle(loop.id, "evaluation-one", { ...emptyResources(), cpu_core_hours: 0.05 }, "SYNTHETIC-local-usage")
    recovered.reserve(loop.id, recovered.get(loop.id).revision, model())
    expect(() => recovered.reserve(loop.id, recovered.get(loop.id).revision, evaluation("evaluation-two"))).toThrow("model call")
  })

  test("evaluation cannot evade the combined CPU ceiling or hide a measured overage", () => {
    const { store, loop } = fixture()
    store.reserve(loop.id, 0, compute("round-one", 1, { cpu_core_hours: 3.95 }))
    expect(() => store.reserve(loop.id, store.get(loop.id).revision, evaluation())).toThrow("Cumulative research budget exceeded: cpu_core_hours")
    store.reserve(loop.id, store.get(loop.id).revision, evaluation("evaluation-one", 0.02))
    const blocked = store.settle(loop.id, "evaluation-one", { ...emptyResources(), cpu_core_hours: 0.03 }, "SYNTHETIC-measured-overage")
    expect(blocked.state).toBe("BLOCKED")
    expect(blocked.ledger.used.cpu_core_hours).toBe(0.03)
    expect(blocked.ledger.reserved.cpu_core_hours).toBe(3.95)
  })

  test("evaluation requires explicit scientific scope and cannot reserve GPU, model work or candidate slots", () => {
    const offline = fixture({ ...contract(), mode: "offline-replay" })
    expect(() => offline.store.reserve(offline.loop.id, 0, evaluation())).toThrow("Offline replay")
    const disallowed = fixture({ ...contract(), allowed_adaptations: ["inspect", "stop"] })
    expect(() => disallowed.store.reserve(disallowed.loop.id, 0, evaluation())).toThrow("not allowed")
    const { store, loop } = fixture()
    for (const item of [
      { ...evaluation(), round: 1 }, { ...evaluation(), candidate_slots: 1 }, evaluation("zero-cpu", 0),
      ...(["gpu_hours", "compute_points", "model_tokens", "model_cost"] as const).map(key => ({
        ...evaluation(), resources: { ...evaluation().resources, [key]: 1 },
      })),
    ]) expect(() => store.reserve(loop.id, 0, item)).toThrow("Scientific evaluation reserves only")
    expect(store.reservations(loop.id)).toHaveLength(0)
  })

  test("local evaluation cannot gain native submission rights or certify completion with unresolved CPU usage", () => {
    const { store, loop } = fixture()
    store.reserve(loop.id, 0, evaluation())
    expect(() => store.bindTask(loop.id, "evaluation-one", "native-task", evaluation().plan_digest)).toThrow("no matching")
    expect(() => store.claimSubmission(loop.id, "evaluation-one", store.get(loop.id).revision, evaluation().plan_digest)).toThrow("no matching")
    store.record(loop.id, "SYNTHETIC-scientific-result", { synthetic: true })
    expect(() => store.finish(loop.id, store.get(loop.id).revision, "objective_met", ["SYNTHETIC-scientific-result"])).toThrow("settled scientific computation")
    store.finish(loop.id, store.get(loop.id).revision, "user_cancelled", [])
    const settled = store.settle(loop.id, "evaluation-one", { ...emptyResources(), cpu_core_hours: 0.02 }, "SYNTHETIC-late-local-usage")
    expect(settled.state).toBe("CANCELLED")
    expect(settled.ledger.used.cpu_core_hours).toBe(0.02)
  })
})

describe("submission identity, cancellation and late results", () => {
  test("claims submission only once across connections and process recovery", () => {
    const { store, loop, connect } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    store.bindTask(loop.id, input.key, "native-draft-one", input.plan_digest)
    const revision = store.get(loop.id).revision
    const first = store.claimSubmission(loop.id, input.key, revision, input.plan_digest)
    expect(first.claimed).toBe(true)
    expect(first.reservation.task).toBe("native-draft-one")
    const recovered = connect().claimSubmission(loop.id, input.key, revision, input.plan_digest)
    expect(recovered.claimed).toBe(false)
    expect(recovered.submission).toEqual(first.submission)
    expect(store.events(loop.id).filter(event => event.kind === "submission_claimed")).toHaveLength(1)
    expect(() => store.claimSubmission(loop.id, input.key, store.get(loop.id).revision, researchDigest("different-plan"))).toThrow("frozen resource")
  })

  test("a claim without a task remains uncertain and never becomes a second submission", () => {
    const { store, loop, connect } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    const first = store.claimSubmission(loop.id, input.key, store.get(loop.id).revision, input.plan_digest)
    const retry = connect().claimSubmission(loop.id, input.key, store.get(loop.id).revision, input.plan_digest)
    expect(first.claimed).toBe(true)
    expect(retry.claimed).toBe(false)
    expect(retry.reservation.task).toBeNull()
  })

  test("a later model overage prevents submission of an already reserved compute plan", () => {
    const { store, loop } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    store.reserve(loop.id, store.get(loop.id).revision, model())
    store.settle(loop.id, "model-one", { ...emptyResources(), model_tokens: 2500 }, "verified:overage")
    const observed = store.observe(loop.id, "new:evidence", { status: "available" })
    expect(() => store.claimSubmission(loop.id, input.key, observed.revision, input.plan_digest)).toThrow("breach")
    expect(store.events(loop.id).filter(event => event.kind === "submission_claimed")).toEqual([])
  })

  test("one reservation binds one task and one task cannot represent different rounds", () => {
    const { store, loop } = fixture()
    const first = compute()
    store.reserve(loop.id, 0, first)
    store.bindTask(loop.id, first.key, "native-one", first.plan_digest)
    store.bindTask(loop.id, first.key, "native-one", first.plan_digest)
    expect(store.events(loop.id).filter(event => event.kind === "task_bound")).toHaveLength(1)
    expect(() => store.bindTask(loop.id, first.key, "native-two", first.plan_digest)).toThrow("another task")
    expect(() => store.bindTask(loop.id, first.key, "native-one", researchDigest("other"))).toThrow("frozen")
    const settled = store.settle(loop.id, first.key, emptyResources(), "verified:one")
    const second = compute("round-two", 2)
    store.reserve(loop.id, settled.revision, second)
    expect(() => store.bindTask(loop.id, second.key, "native-one", second.plan_digest)).toThrow("another reservation")
  })

  test("cancellation prevents new claims but preserves late task receipts, observations and actual cost", () => {
    const { store, loop } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    const cancelled = store.finish(loop.id, store.get(loop.id).revision, "user_cancelled", [])
    expect(() => store.claimSubmission(loop.id, input.key, cancelled.revision, input.plan_digest)).toThrow("terminal")
    // This is receipt binding, not authorization to submit after cancellation.
    store.bindTask(loop.id, input.key, "already-running-native", input.plan_digest)
    const observed = store.observe(loop.id, "late:result", { task: "already-running-native", status: "completed" })
    expect(observed.state).toBe("CANCELLED")
    expect(observed.outcome).toBe("user_cancelled")
    const settled = store.settle(loop.id, input.key, { ...emptyResources(), gpu_hours: 0.12 }, "verified:late-usage")
    expect(settled.state).toBe("CANCELLED")
    expect(settled.ledger.used.gpu_hours).toBe(0.12)
    expect(() => store.reserve(loop.id, settled.revision, compute("round-two", 2))).toThrow("terminal")
    expect(() => store.decide(loop.id, settled.revision, { action: "prepare_action" })).toThrow("terminal")
  })

  test("objective completion requires evidence and settled scientific computation", () => {
    const { store, loop } = fixture()
    expect(() => store.finish(loop.id, 0, "objective_met", [])).toThrow("requires evidence")
    expect(() => store.finish(loop.id, 0, "objective_met", ["unknown"])).toThrow("Unknown completion")
    store.reserve(loop.id, 0, compute())
    const observed = store.observe(loop.id, "result:one", { quality: "pass" })
    expect(() => store.finish(loop.id, observed.revision, "objective_met", ["result:one"])).toThrow("settled scientific")
    const settled = store.settle(loop.id, "round-one", emptyResources(), "verified:receipt")
    const finished = store.finish(loop.id, settled.revision, "objective_met", ["result:one"])
    const late = store.observe(loop.id, "late:detail", { count: 5 })
    expect(late.state).toBe("FINISHED")
    expect(late.outcome).toBe(finished.outcome)
    expect(() => store.transition(loop.id, late.revision, "READY", {})).toThrow("terminal")
  })
})

describe("SQLite concurrency and integrity", () => {
  test("two real concurrent processes race for one durable submission claim", async () => {
    const { store, loop, filename } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    const revision = store.get(loop.id).revision
    const script = `import { ResearchLoopStore } from ${JSON.stringify(path.join(import.meta.dir, "research-loop-store.ts"))};
      const store = new ResearchLoopStore(process.env.TEST_RESEARCH_DB);
      const result = store.claimSubmission(process.env.TEST_RESEARCH_LOOP, "round-one", ${revision}, ${JSON.stringify(input.plan_digest)});
      console.log(JSON.stringify({claimed:result.claimed})); store.close();`
    const children = [0, 1].map(() => Bun.spawn({ cmd: [process.execPath, "-e", script],
      env: { ...process.env, TEST_RESEARCH_DB: filename, TEST_RESEARCH_LOOP: loop.id }, stdout: "pipe", stderr: "pipe" }))
    const results = await Promise.all(children.map(async child => ({
      code: await child.exited, output: await new Response(child.stdout).text(), error: await new Response(child.stderr).text(),
    })))
    expect(results.map(result => ({ code: result.code, error: result.error }))).toEqual([{ code: 0, error: "" }, { code: 0, error: "" }])
    expect(results.map(result => JSON.parse(result.output).claimed).sort()).toEqual([false, true])
    expect(store.events(loop.id).filter(event => event.kind === "submission_claimed")).toHaveLength(1)
  })

  test("concurrent controllers cannot each reserve against the same revision", () => {
    const { store, loop, connect } = fixture()
    const second = connect()
    store.reserve(loop.id, loop.revision, model())
    expect(() => second.reserve(loop.id, loop.revision, model("model-two"))).toThrow("Stale")
    expect(second.get(loop.id).ledger.total.model_tokens).toBe(2000)
    expect(second.reservations(loop.id)).toHaveLength(1)
  })

  test("detects overwritten contract, evidence and hash-chained events", () => {
    const { store, loop } = fixture()
    store.observe(loop.id, "one", { count: 1 })
    store.db.query("UPDATE research_evidence SET payload=? WHERE loop=? AND key=?").run('{"count":2}', loop.id, "one")
    expect(() => store.evidence(loop.id)).toThrow("evidence changed")
    store.db.query("UPDATE research_events SET payload=? WHERE loop=? AND kind='observation'").run('{"key":"changed"}', loop.id)
    expect(() => store.events(loop.id)).toThrow("history integrity")
    store.db.query("UPDATE research_loops SET contract=? WHERE id=?").run(JSON.stringify({ ...contract(), objective: "tampered" }), loop.id)
    expect(() => store.get(loop.id)).toThrow("contract changed")
  })

  test("settlement and task changes must match immutable events", () => {
    const { store, loop } = fixture()
    const input = compute()
    store.reserve(loop.id, 0, input)
    store.bindTask(loop.id, input.key, "native-one", input.plan_digest)
    store.settle(loop.id, input.key, { ...emptyResources(), gpu_hours: 0.1 }, "verified:one")
    store.db.query("UPDATE research_reservations SET actual=? WHERE loop=? AND key=?")
      .run(JSON.stringify(emptyResources()), loop.id, input.key)
    expect(() => store.ledger(loop.id)).toThrow("immutable receipt event")
    store.db.query("UPDATE research_reservations SET actual=?,task=? WHERE loop=? AND key=?")
      .run(JSON.stringify({ ...emptyResources(), gpu_hours: 0.1 }), "native-other", loop.id, input.key)
    expect(() => store.ledger(loop.id)).toThrow("binding has no matching")
  })

  test("content hashes cannot be reassigned to a different evidence identity", () => {
    const { store, loop } = fixture()
    store.observe(loop.id, "original", { count: 1 })
    store.db.query("UPDATE research_evidence SET key=? WHERE loop=? AND key=?").run("different", loop.id, "original")
    expect(() => store.evidence(loop.id)).toThrow("identity has no matching")
  })
})
