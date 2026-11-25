import { beforeEach, afterEach, afterAll, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import { mkdtemp, realpath, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { Store } from "../inspire-screening/executor/store"
import { requestHash } from "../inspire-screening/executor/request"
import { digest, type Outcome, type Phase } from "../inspire-screening/executor/model"
import { deliveryPath } from "../inspire-screening/executor/delivery"
import { ResearchLoopStore } from "./research-loop-store"
import { researchDigest, type ResearchContract } from "./research-loop-contract"
import { compileResearchExecution, prepareResearchExecution, type ResearchExecutionEnvironment } from "./research-execution"
import { writeSyntheticResearchProofs } from "./research-execution-test-fixture"
import { researchSnapshot, commitResearchDecision } from "./research-loop"
import { registerResearchNativePlan, readResearchNativeAction, nativeResearchCapability } from "./research-native-controller"
import { observeResearchNativeAction } from "./research-native-observation"
import { createResearchObservation, verifyResearchObservation } from "./research-observations"

const scratch = await realpath(await mkdtemp(path.join(tmpdir(), "research-native-observation-")))
afterAll(() => rm(scratch, { recursive: true, force: true }))
const hash = (value: string) => createHash("sha256").update(value).digest("hex")

// All native execution/approval/publication rows below are explicit SYNTHETIC
// SQLite fixtures, never real model, remote execution, publication or approval.
async function setup(explicit = false) {
  const data = await fixture()
  if (explicit) {
    const original = data.binding.library
    const prefix = "/inspire/SYNTHETIC-inputs/prefix.csv", receptor = "/inspire/SYNTHETIC-inputs/prepared.pdb"
    const reference = "/inspire/SYNTHETIC-inputs/reference.sdf", manifest = "/inspire/SYNTHETIC-inputs/subset.json"
    Object.assign(data.binding, { library: prefix, receptor, reference, subset_remote_manifest: manifest })
    Object.assign(data.environment.assets, { [prefix]: data.proofs.subset.subset.sha256, [receptor]: data.contract.target.receptor_sha256!,
      [reference]: hash("SYNTHETIC reference ligand"), [manifest]: data.contract.inputs.subset_sha256! })
    data.environment.research_inputs = { version: "screening-research-inputs-v1",
      library: { path: prefix, sha256: data.proofs.subset.subset.sha256, records: 20000, selection: "frozen-file-order-prefix",
        source: { path: original, sha256: data.contract.inputs.library_sha256! }, manifest: { path: manifest, sha256: data.contract.inputs.subset_sha256! } },
      receptor: { path: receptor, sha256: data.contract.target.receptor_sha256! }, reference: { path: reference, sha256: data.environment.assets[reference]! },
      science_config: { path: data.binding.science_config, sha256: data.contract.inputs.science_config_sha256! } }
  }
  const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
  const native = new Store(path.join(data.directory, "native.sqlite"))
  const loop = research.create(data.contract)
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding,
    { finish_before: loop.created + loop.contract.limits.max_elapsed_ms })
  const offered = await registerResearchNativePlan({ research, id: loop.id, key: "round-1", round: 1, prepared })
  research.record(loop.id, "synthetic-receipt", { schema: "synthetic-model-receipt-fixture", model_called: false })
  const snapshot = researchSnapshot(research, loop.id)
  await commitResearchDecision({ store: research, id: loop.id, context: snapshot.context, expected_revision: snapshot.loop.revision,
    provenance: { kind: "model_response", receipt_ref: "synthetic-receipt", request_digest: hash("synthetic") },
    value: { schema: "drug-design-decision-v1", loop_id: loop.id, state_revision: snapshot.loop.revision,
      action: "execute_prepared_action", capability_id: nativeResearchCapability, plan_id: prepared.plan.id,
      arguments: {}, evidence_refs: [offered.evidence_ref], rationale: "SYNTHETIC control-path fixture, not a model call",
      alternatives: [], expected_observation: "Synthetic test only", outcome: null, missing_inputs: [] } })
  const identity = { research, native, id: loop.id }
  const reference = readResearchNativeAction(identity).reference
  await prepareResearchExecution(native, prepared, reference)
  research.bindTask(loop.id, "round-1", prepared.plan.id, prepared.plan_digest)
  research.claimSubmission(loop.id, "round-1", research.get(loop.id).revision, prepared.plan_digest)
  native.approve(prepared.plan.id, prepared.plan_digest, data.contract.session, data.directory)
  research.transition(loop.id, research.get(loop.id).revision, "RUNNING", { synthetic_fixture: true, remote_compute: false })
  return { ...data, ...identity, prepared, reference, close: () => { native.close(); research.close() } }
}

type Fixture = Awaited<ReturnType<typeof setup>>
function selection(data: Fixture, zero = false) {
  return { schema_version: "1.0", algorithm_version: "covindb-prescreen-v1.2", diagnostics_version: "prescreen-rejections-v1",
    status: "completed", selection_outcome: zero ? "NO_CANDIDATES_SELECTED" : "CANDIDATES_SELECTED",
    source: { path: data.binding.library, sha256: data.prepared.plan.research_inputs?.library.sha256 ?? data.contract.inputs.library_sha256, size_bytes: 100000,
      max_rows: 20000, records_read: 20000, record_selection: "file_order_prefix" },
    target: { config_path: data.binding.science_config, config_sha256: data.contract.inputs.science_config_sha256,
      target_id: "EGFR", site_id: "EGFR_C797", reaction_type: "Michael Addition", warhead_class: "acrylamide" },
    selection: { top_n_requested: 5, top_n_selected: zero ? 0 : 5 },
    counts: { input_rows: 20000, selected: zero ? 0 : 5, parsed: 20000, unique: 20000, duplicates: 0, invalid: 0, hard_gate_pass: zero ? 0 : 5 },
    quality: { identity_loss_count: 0, failure_table_present: true, rdkit_parse_rate: 1, selected_warhead_mapping_rate: zero ? 0 : 1 },
    quality_gates: { rdkit_parse_rate_ge_0_99: true, selected_warhead_mapping_rate_ge_0_98: !zero, identity_loss_zero: true, failure_table_present: true },
    synthetic_fixture: "Not an actual research result" }
}

async function publish(data: Fixture, phase: Phase, name: string, text: string) {
  const task = data.native.get(data.prepared.plan.id)
  const sha256 = hash(text), bytes = Buffer.byteLength(text)
  const file = await deliveryPath(task, { phase, name, sha256 })
  await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, text)
  data.native.db.query("INSERT INTO artifact_publications VALUES(?,?,?,?,?)").run(task.id, phase, name, task.digest,
    JSON.stringify({ phase, name, artifact_id: `art_${hash(name).slice(0, 12)}`, version_id: `ver_${hash(name).slice(0, 12)}`,
      project_id: "synthetic-fixture-project", sha256, bytes, published_at: 1000, evidence_kind: ["error.json", "started.json", "zero-candidates.json", "stage2_selection_manifest.json"].includes(name)
        && task.state === "FAILED" ? "failure_diagnostic" : "core_result" }))
  return file
}

async function success(data: Fixture, options: { missing?: string; wrongSource?: boolean; wrongIdentity?: boolean; wrongCount?: boolean; originalSource?: boolean } = {}) {
  const source = selection(data)
  if (options.wrongSource) source.source.sha256 = "a".repeat(64)
  if (options.originalSource) Object.assign(source.source, data.prepared.plan.research_inputs!.library.source)
  const csv = "compound_id,input_sdf_index,standardized_smiles\n" + Array.from({ length: 5 }, (_, i) => `SYNTHETIC-${i},${i},C=CC(=O)N\n`).join("")
  const engine = (label: string, direction: string) => "compound_id,input_sdf_index,standardized_smiles,engine,score_direction,engine_status,raw_score\n"
    + Array.from({ length: 5 }, (_, i) => `SYNTHETIC-${i},${i},C=CC(=O)N,${label},${direction},SUCCESS,${i + 1}\n`).join("")
  const sdf = Array.from({ length: 5 }, (_, i) => `SYNTHETIC NOT A MOLECULE\n> <COMPOUND_ID>\nSYNTHETIC-${i}\n\n> <INPUT_SDF_INDEX>\n${i}\n\n$$$$\n`).join("")
  const files: Record<string, Record<string, string>> = { prepare: { "stage2_selection_manifest.json": JSON.stringify(source), "stage2_candidates.csv": csv, "stage2_candidates.sdf": sdf },
    carsidock: { "scores.csv": engine("carsidock-cov", "lower_is_better") }, rtmscore: { "scores.csv": engine("rtmscore", "higher_is_better") },
    report: { "candidates.csv": "compound_id,input_sdf_index,standardized_smiles,carsidock_status,rtmscore_status,carsiscore,rtmscore\n"
      + Array.from({ length: 5 }, (_, i) => `SYNTHETIC-${i},${i},C=CC(=O)N,SUCCESS,SUCCESS,${i + 1},${i + 1}\n`).join("") } }
  if (options.wrongIdentity) files.rtmscore!["scores.csv"] = files.rtmscore!["scores.csv"]!.replace("SYNTHETIC-0", "SYNTHETIC-imposter")
  const task = data.native.get(data.prepared.plan.id)
  const identity = hash(JSON.stringify(Array.from({ length: 5 }, (_, i) => [i, `SYNTHETIC-${i}`])))
  const manifests: Outcome[] = []
  const paths: Record<string, string> = {}
  for (const stage of data.native.stages(task.id)) {
    const content = files[stage.phase]!
    const manifest: Outcome = { task_id: task.id, phase: stage.phase, plan_digest: task.digest, status: "SUCCEEDED",
      parent_digest: manifests.length ? digest(manifests.at(-1)) : null, input_count: 5,
      success_count: options.wrongCount && stage.phase === "rtmscore" ? 4 : 5,
      failed_count: options.wrongCount && stage.phase === "rtmscore" ? 1 : 0,
      identity_digest: identity, gates: { synthetic_fixture_not_science: true }, runtime_seconds: 1,
      artifacts: Object.fromEntries(Object.entries(content).map(([name, text]) => [name, hash(text)])) }
    manifests.push(manifest)
    data.native.stage(task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest), submitted: 1000 })
    for (const [name, text] of Object.entries(content)) {
      if (options.missing === `${stage.phase}/${name}`) continue
      paths[`${stage.phase}/${name}`] = await publish(data, stage.phase, name, text)
    }
  }
  data.native.state(task.id, "SUCCEEDED")
  return paths
}

async function failure(data: Fixture, options: { zero?: boolean; wrongPlan?: boolean; genericError?: boolean; missingDiagnostic?: boolean } = {}) {
  const task = data.native.get(data.prepared.plan.id)
  data.native.stage(task.id, 0, { state: "FAILED", submitted: 1000 })
  data.native.state(task.id, "FAILED", "SYNTHETIC failure; zero candidates words are not evidence")
  const selected = selection(data, true)
  const selectionText = JSON.stringify(selected)
  const files: Record<string, string> = {
    "started.json": JSON.stringify({ task_id: task.id, phase: "prepare", plan_digest: task.digest, started_at: 1 }),
    "error.json": JSON.stringify({ task_id: task.id, phase: "prepare", plan_digest: task.digest, status: "FAILED", runtime_seconds: 1,
      error: "SYNTHETIC fixture. This text is not a classifier input.", ...(options.zero && !options.genericError ? { failure_kind: "zero_candidates", exit_code: 2 } : {}) }),
  }
  if (options.zero) {
    const parquet = { sha256: hash("SYNTHETIC remote parquet witness, not actual molecule data"), bytes: 100 }
    files["stage2_selection_manifest.json"] = selectionText
    files["zero-candidates.json"] = JSON.stringify({ version: "screening-zero-candidates-v1", task_id: task.id, phase: "prepare",
      plan_digest: options.wrongPlan ? "f".repeat(64) : task.digest, outcome: "NO_CANDIDATES_SELECTED", candidate_count: 0, input_rows: 20000,
      actual_source: selected.source, target_config: selected.target, termination: { kind: "prescreen_quality_gate_exit", exit_code: 2 },
      files: { "stage2_selection_manifest.json": { sha256: hash(selectionText), bytes: Buffer.byteLength(selectionText) },
        "stage2_candidates.parquet": parquet, "covindb_master.parquet": parquet, "covindb_rejected.parquet": parquet },
      input_quality_verified: true, scientific_quality_verified: false, activity_claim: "SYNTHETIC fixture; no real activity claim" })
  }
  data.native.event(task.id, "failure_evidence_witness", { version: "screening-failure-evidence-v1", task_id: task.id, plan_digest: task.digest,
    phase: "prepare", status: "FAILED", started_at: 1, runtime_seconds: 1,
    artifacts: Object.fromEntries(Object.entries(files).map(([name, text]) => [name, { sha256: hash(text), bytes: Buffer.byteLength(text) }])),
    scientific_quality_verified: false, contents_included: false })
  for (const [name, text] of Object.entries(files)) if (!(options.missingDiagnostic && name === "zero-candidates.json")) await publish(data, "prepare", name, text)
}

test("actual SQLite Results adapter reconstructs selection and stage identities, leaving science and usage unknown", async () => {
  const data = await setup()
  try {
    await success(data)
    const value = await observeResearchNativeAction(data)
    expect(value.observation).toMatchObject({ origin: "native_execution", new_compute: true, classification: "incomplete",
      run: { task_id: data.prepared.plan.id, status: "succeeded", phase: "report" }, identity: { status: "pass" },
      counts: { input_records: 20000, selected: 5, attempted: 5, execution_succeeded: 5, scientific_passed: null }, integrity_errors: [] })
    expect(value.observation!.candidates.every(item => item.scores.length === 2 && item.scientific_status === "unknown")).toBe(true)
    expect(value.observation!.scientific_checks.every(item => item.status === "unknown" && item.evaluator === null)).toBe(true)
    expect(value.loop.state).toBe("DECIDING")
    expect(value.accounting_status).toBe("RESERVED")
    expect(data.research.reservations(data.id)[0]!.actual).toBeNull()
    expect(verifyResearchObservation(value.observation)).toEqual(value.observation!)
    const before = data.research.get(data.id).revision
    const again = await observeResearchNativeAction(data)
    expect(again.observation).toEqual(value.observation!)
    expect(again.loop.revision).toBe(before)
    expect(data.research.evidence(data.id, "observation")).toHaveLength(1)
    const nativeDecision = readResearchNativeAction(data).decision_id
    data.research.record(data.id, "synthetic-inspection-receipt", { schema: "synthetic-model-receipt-fixture", model_called: false })
    const snapshot = researchSnapshot(data.research, data.id)
    await commitResearchDecision({ store: data.research, id: data.id, context: snapshot.context, expected_revision: snapshot.loop.revision,
      provenance: { kind: "model_response", receipt_ref: "synthetic-inspection-receipt", request_digest: hash("synthetic-inspection") },
      value: { schema: "drug-design-decision-v1", loop_id: data.id, state_revision: snapshot.loop.revision,
        action: "inspect_evidence", capability_id: "research.inspect-recorded-artifacts.v1", plan_id: null,
        arguments: { observation_id: value.observation!.id }, evidence_refs: [value.observation!.id], rationale: "SYNTHETIC local inspection",
        alternatives: [], expected_observation: "Only local byte hashes", outcome: null, missing_inputs: [] } })
    expect(readResearchNativeAction(data).decision_id).toBe(nativeDecision)
    expect((await observeResearchNativeAction(data)).observation).toEqual(value.observation!)
    expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
  } finally { data.close() }
})

test("missing published results stay incomplete and do not adopt stage counts as parsed candidate facts", async () => {
  const data = await setup()
  try {
    await success(data, { missing: "prepare/stage2_candidates.csv" })
    const { observation } = await observeResearchNativeAction(data)
    expect(observation).toMatchObject({ classification: "incomplete", identity: { status: "unknown" }, counts: { selected: null, execution_succeeded: null } })
    expect(observation!.candidates).toEqual([])
    expect(observation!.artifacts.some(item => item.status === "missing")).toBe(true)
  } finally { data.close() }
})

test.each([false, true])("actual frozen prefix and original full library cannot be interchanged: substitution=%s", async originalSource => {
  const data = await setup(true)
  try {
    await success(data, { originalSource })
    const { observation } = await observeResearchNativeAction(data)
    expect(observation!.classification).toBe(originalSource ? "integrity_failure" : "incomplete")
    expect(observation!.identity.status).toBe(originalSource ? "fail" : "pass")
    if (originalSource) expect(observation!.integrity_errors).toContain("native_selection_source_or_scope_mismatch")
    else expect(observation!.integrity_errors).toEqual([])
  } finally { data.close() }
})

test.each(["wrongSource", "wrongIdentity", "wrongCount"] as const)("hash-valid but semantically inconsistent Results fail integrity: %s", async defect => {
  const data = await setup()
  try {
    await success(data, { [defect]: true })
    expect((await observeResearchNativeAction(data)).observation!.classification).toBe("integrity_failure")
  } finally { data.close() }
})

test("changed delivered bytes and deleted Results are detected by fresh capture", async () => {
  const data = await setup()
  try {
    const files = await success(data)
    await writeFile(files["rtmscore/scores.csv"]!, "changed")
    expect((await observeResearchNativeAction(data)).observation!.classification).toBe("integrity_failure")
    await rm(files["rtmscore/scores.csv"]!)
    const result = await observeResearchNativeAction(data)
    expect(result.observation!.classification).toBe("incomplete")
    expect(result.observation!.missing_evidence.some(value => value.startsWith("artifact:"))).toBe(true)
  } finally { data.close() }
})

test("ordinary failed task is execution_failure even when error text says zero candidates", async () => {
  const data = await setup()
  try {
    await failure(data)
    expect((await observeResearchNativeAction(data)).observation).toMatchObject({ classification: "execution_failure", run: { status: "failed" }, counts: { selected: null } })
  } finally { data.close() }
})

test("paired zero diagnostic preserves native FAILED, reports a completed empty selection and keeps science unknown", async () => {
  const data = await setup()
  try {
    await failure(data, { zero: true })
    const { observation } = await observeResearchNativeAction(data)
    expect(observation).toMatchObject({ classification: "zero_candidates", run: { status: "failed", phase: "prepare" }, identity: { status: "pass" },
      counts: { input_records: 20000, selected: 0, attempted: 0, execution_succeeded: 0, scientific_passed: null }, failure: null, integrity_errors: [] })
    expect(data.native.get(data.prepared.plan.id).state).toBe("FAILED")
    expect(data.native.stages(data.prepared.plan.id).slice(1).every(stage => stage.state === "WAITING" && stage.submitted === null)).toBe(true)
    expect(observation!.missing_evidence).toContain("scientific_passed_count")
    expect(verifyResearchObservation(observation)).toEqual(observation!)
    const { schema: _schema, id: _id, classification: _classification, missing_evidence: _missing, evidence_refs: _refs, new_compute: _new, ...body } = observation!
    expect(createResearchObservation({ ...body, failure: { category: "environment", detail: "Additional environment failure",
      evidence_refs: body.selection_outcome!.evidence_refs } }).classification).toBe("execution_failure")
    expect(createResearchObservation({ ...body, origin: "historical_import" }).classification).toBe("execution_failure")
    expect(createResearchObservation({ ...body, selection_outcome: undefined }).classification).toBe("execution_failure")
    expect(createResearchObservation({ ...body, counts: { ...body.counts, input_records: null } }).classification).toBe("execution_failure")
  } finally { data.close() }
})

test.each(["wrongPlan", "genericError", "missingDiagnostic"] as const)("zero selection cannot override missing or mismatched structured evidence: %s", async defect => {
  const data = await setup()
  try {
    await failure(data, { zero: true, [defect]: true })
    const value = (await observeResearchNativeAction(data)).observation!
    expect(value.classification).toBe(defect === "missingDiagnostic" ? "execution_failure" : "integrity_failure")
    expect(value.selection_outcome).toBeUndefined()
  } finally { data.close() }
})

test("late terminal evidence cannot revive a cancelled loop and reopening cannot create a second observation", async () => {
  const data = await setup()
  try {
    await success(data)
    data.research.finish(data.id, data.research.get(data.id).revision, "user_cancelled", [])
    const first = await observeResearchNativeAction(data)
    expect(first.loop.state).toBe("CANCELLED")
    const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
    const native = new Store(path.join(data.directory, "native.sqlite"))
    try {
      const second = await observeResearchNativeAction({ research, native, id: data.id })
      expect(second.observation).toEqual(first.observation!)
      expect(second.loop).toEqual(first.loop)
      expect(research.evidence(data.id, "observation")).toHaveLength(1)
      expect(native.list()).toHaveLength(1)
      expect(research.reservations(data.id)[0]!.actual).toBeNull()
    } finally { native.close(); research.close() }
  } finally { data.close() }
})

test("unfinished native task produces only a readback receipt and no invented observation", async () => {
  const data = await setup()
  try {
    expect(await observeResearchNativeAction(data)).toMatchObject({ observation: null, evidence_ref: null, accounting_status: "RESERVED" })
    expect(data.research.evidence(data.id, "observation")).toEqual([])
    expect(data.native.get(data.prepared.plan.id).state).toBe("APPROVED")
  } finally { data.close() }
})

async function fixture() {
  const directory = await realpath(await mkdtemp(path.join(scratch, "case-")))
  const shared = "/inspire/fixture-shared"
  const binding = { library: `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv`,
    receptor: `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb`,
    science_config: `/inspire/fixture/releases/${"3".repeat(64)}/science/configs/covindb/EGFR_C797_mvp_v1.yaml`, mapping: path.join(directory, "mapping.json"),
    subset_manifest: path.join(directory, "subset.json"), quality_manifest: path.join(directory, "quality.json") }
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "fixture-project", session: "fixture-session",
    objective: "Test native plan binding, not chemistry", mode: "native-compute", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: "2".repeat(64), mapping_sha256: hash("synthetic-test-proof") },
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", library_sha256: "1".repeat(64), subset_sha256: hash("synthetic-test-proof"),
      source_release: "3".repeat(64), science_config_sha256: "4".repeat(64) },
    quality: { manifest_sha256: hash("synthetic-test-proof"), required_checks: ["synthetic_gate"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 8, compute_points: 2, model_tokens: 40000, model_cost: 1 },
      max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
      max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 12, max_elapsed_ms: 7200000 },
    allowed_adaptations: ["inspect", "stop"] }
  const proofs = await writeSyntheticResearchProofs(contract, binding)
  const release = `/inspire/fixture/releases/${contract.inputs.source_release}`
  const request = "Synthetic research execution adapter test, not authorized remote computation"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: contract.project, group: "CPU",
    quota: "0,4,16", image: "fixture-image", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0.3 }
  const environment: ResearchExecutionEnvironment = {
    source: { text: request, origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant",
      part_ids: ["fixture-part"], request_sha256: requestHash(request) } },
    connection: { cli: "/nonexistent/fixture-cli", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    runtime: { root: "/inspire/fixture", project: `${release}/science`, worker: `${release}/worker.py`, activation: `${release}/science/environment/activate_carsidock.sh` },
    assets: { [binding.library]: contract.inputs.library_sha256!, [binding.receptor]: contract.target.receptor_sha256!,
      [binding.science_config]: contract.inputs.science_config_sha256!, [`${release}/worker.py`]: "5".repeat(64), [`${release}/manifest.json`]: "6".repeat(64) },
    cpu, gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 2 } }
  return { directory, contract, environment, binding, proofs, ref: { loop: "loop-fixture", key: "round-1", contract_digest: researchDigest(contract) } }
}
