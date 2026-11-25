import { beforeEach, afterEach, afterAll, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { Store } from "../inspire-screening/executor/store"
import { digest, type Outcome } from "../inspire-screening/executor/model"
import { requestHash } from "../inspire-screening/executor/request"
import { writeSyntheticResearchProofs } from "./research-execution-test-fixture"
import { researchDigest, type ResearchContract } from "./research-loop-contract"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"
import { compileResearchExecution, prepareResearchExecution, readResearchExecution,
  researchExecutionResources, verifyPreparedResearchExecution, type ResearchExecutionEnvironment } from "./research-execution"

const scratch = await realpath(await mkdtemp(path.join(tmpdir(), "research-execution-")))
afterAll(() => rm(scratch, { recursive: true, force: true }))
const hash = (value: string) => createHash("sha256").update(value).digest("hex")

async function sealProofs(data: Awaited<ReturnType<typeof fixture>>) {
  data.contract.target.mapping_sha256 = hash(JSON.stringify(data.proofs.audit))
  data.contract.inputs.subset_sha256 = hash(JSON.stringify(data.proofs.subset))
  data.proofs.quality.scope.dataset_manifest_sha256 = data.contract.inputs.subset_sha256
  data.contract.quality.manifest_sha256 = hash(JSON.stringify(data.proofs.quality))
  await writeFile(data.binding.mapping, JSON.stringify(data.proofs.audit))
  await writeFile(data.binding.subset_manifest, JSON.stringify(data.proofs.subset))
  await writeFile(data.binding.quality_manifest, JSON.stringify(data.proofs.quality))
}

const admissionCases: { name: string; change: (data: Awaited<ReturnType<typeof fixture>>) => void; error: string }[] = [
  { name: "mapping pass does not override failed receptor audit", change: data => { data.proofs.audit.status = "fail" }, error: "audit must pass" },
  { name: "unknown mapping", change: data => { data.proofs.audit.mapping_status = "unknown" }, error: "audit must pass" },
  { name: "failed coordinate check under a pass summary", change: data => { data.proofs.audit.checks[4]!.status = "fail" }, error: "audit must pass" },
  { name: "missing coordinate check", change: data => { data.proofs.audit.checks.pop() }, error: "audit must pass" },
  { name: "duplicated audit check", change: data => { data.proofs.audit.checks.push(data.proofs.audit.checks[0]!) }, error: "audit must pass" },
  { name: "different audited structure", change: data => { data.proofs.audit.structure_sha256 = "7".repeat(64) }, error: "exact receptor" },
  { name: "different author residue", change: data => { data.proofs.audit.author_residue_id = "797" }, error: "exact receptor" },
  { name: "quality policy for another receptor", change: data => { data.proofs.quality.target.receptor_sha256 = "8".repeat(64) }, error: "quality policy differs" },
  { name: "quality policy for another chain", change: data => { data.proofs.quality.target.chain = "B" }, error: "quality policy differs" },
  { name: "quality policy for another recipe", change: data => { data.proofs.quality.recipe.sha256 = "8".repeat(64) }, error: "quality policy differs" },
  { name: "quality policy for another evaluator", change: data => { data.proofs.quality.evaluator.sha256 = "8".repeat(64) }, error: "quality policy differs" },
  { name: "unsupported quality contract", change: data => { data.contract.quality.required_checks = ["synthetic_gate"] }, error: "quality requirements" },
  { name: "subset from another source", change: data => { data.proofs.subset.source.sha256 = "8".repeat(64) }, error: "Frozen subset" },
  { name: "subset from another source path", change: data => { data.proofs.subset.source.path = "/inspire/another.csv" }, error: "Frozen subset" },
  { name: "wrong subset count", change: data => { data.proofs.subset.selection.records = 19999 }, error: "Frozen subset" },
  { name: "incomplete subset records", change: data => { data.proofs.subset.records.pop() }, error: "Frozen subset" },
  { name: "reordered subset records", change: data => { data.proofs.subset.records[1]!.source_record_index = 2 }, error: "Frozen subset" },
  { name: "untrue subset uniqueness", change: data => { data.proofs.subset.selection.unique_row_hashes = 19999 }, error: "Frozen subset" },
  { name: "different subset bytes in policy", change: data => { data.proofs.quality.scope.dataset_subset_sha256 = "8".repeat(64) }, error: "Frozen subset" },
  { name: "different manifest file in policy", change: data => { data.proofs.quality.scope.dataset_manifest_path = "other.json" }, error: "Frozen subset" },
]
for (const item of admissionCases) test(`native admission rejects newly hash-frozen invalid evidence: ${item.name}`, async () => {
  const data = await fixture()
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding)
  item.change(data)
  await sealProofs(data)
  await expect(compileResearchExecution(data.contract, data.environment, data.binding)).rejects.toThrow(item.error)
  // Re-freezing bad content and surrounding digests cannot bypass preparation.
  const store = new Store(":memory:")
  try {
    prepared.contract = data.contract
    prepared.contract_digest = researchDigest(data.contract)
    await expect(prepareResearchExecution(store, prepared, { ...data.ref, contract_digest: prepared.contract_digest })).rejects.toThrow(item.error)
    expect(store.list()).toHaveLength(0)
  } finally { store.close() }
})

test("historical worker roles require explicit installation configuration", async () => {
  const data = await fixture()
  delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  await expect(compileResearchExecution(data.contract, data.environment, data.binding)).rejects.toThrow("COVSCREEN_LEGACY_SHARED_ROOT")
})

test("extra asset hashes cannot substitute for files the fixed worker consumes", async () => {
  const data = await fixture()
  for (const role of ["library", "receptor", "science_config"] as const) {
    const binding = { ...data.binding, [role]: "/inspire/unconsumed-extra-asset" }
    const environment = structuredClone(data.environment)
    environment.assets[binding[role]] = environment.assets[data.binding[role]]!
    await expect(compileResearchExecution(data.contract, environment, binding)).rejects.toThrow("consumed input roles")
  }
})

test("explicit research inputs bind the consumed prefix separately from original library provenance", async () => {
  const data = await fixture()
  const directory = "/inspire/fixture/research-inputs"
  const binding = { ...data.binding, library: `${directory}/library-prefix.csv`, receptor: `${directory}/prepared.pdb`,
    reference: `${directory}/reference.sdf`, subset_remote_manifest: `${directory}/manifest.json` }
  const environment = structuredClone(data.environment)
  environment.assets[binding.library] = data.proofs.subset.subset.sha256
  environment.assets[binding.receptor] = data.contract.target.receptor_sha256!
  environment.assets[binding.reference] = hash("SYNTHETIC reference ligand")
  environment.assets[binding.subset_remote_manifest] = data.contract.inputs.subset_sha256!
  environment.research_inputs = { version: "screening-research-inputs-v1",
    library: { path: binding.library, sha256: environment.assets[binding.library]!, records: 20000,
      selection: "frozen-file-order-prefix", source: { path: data.binding.library, sha256: data.contract.inputs.library_sha256! },
      manifest: { path: binding.subset_remote_manifest, sha256: data.contract.inputs.subset_sha256! } },
    receptor: { path: binding.receptor, sha256: data.contract.target.receptor_sha256! },
    science_config: { path: binding.science_config, sha256: data.contract.inputs.science_config_sha256! },
    reference: { path: binding.reference, sha256: environment.assets[binding.reference]! } }
  const prepared = await compileResearchExecution(data.contract, environment, binding)
  expect(prepared.plan.research_inputs).toEqual(environment.research_inputs)
  expect(await verifyPreparedResearchExecution(prepared)).toEqual(prepared)
  expect(prepared.contract.inputs.library_sha256).toBe(data.proofs.subset.source.sha256)
  expect(prepared.plan.assets[binding.library]).toBe(data.proofs.subset.subset.sha256)
  await expect(compileResearchExecution(data.contract, environment, { ...binding, receptor: data.binding.receptor }))
    .rejects.toThrow("explicit worker binding")
  environment.research_inputs.library.sha256 = "7".repeat(64)
  environment.assets[binding.library] = "7".repeat(64)
  await expect(compileResearchExecution(data.contract, environment, binding)).rejects.toThrow("frozen subset bytes")
})

test("actual v4 scientific evidence compiles in a SYNTHETIC plan environment without submission", async () => {
  const data = await fixture()
  const root = process.env.COVSCREEN_ACCEPTANCE_ROOT ?? path.resolve(import.meta.dir, "../../../../..")
  const manifest = JSON.parse(await readFile(path.join(root, preparedReceptorV4.manifest.path), "utf8"))
  const subset = JSON.parse(await readFile(path.join(root, manifest.scope.dataset_manifest_path), "utf8"))
  data.contract.directory = root
  data.contract.target.receptor_sha256 = manifest.target.receptor_sha256
  data.contract.target.mapping_sha256 = preparedReceptorV4.binding.sha256
  data.contract.inputs.library_sha256 = subset.source.sha256
  data.contract.inputs.subset_sha256 = manifest.scope.dataset_manifest_sha256
  data.contract.inputs.science_config_sha256 = manifest.recipe.sha256
  data.contract.quality = { manifest_sha256: preparedReceptorV4.manifest.sha256, required_checks: manifest.required_scientific_checks,
    minimum_candidates: manifest.scope.minimum_accepted_candidates, conclusion: "computational-shortlist-only" }
  const prefix = "/inspire/SYNTHETIC-v4-compiler-test"
  const binding = { ...data.binding, library: `${prefix}/prefix.csv`, receptor: `${prefix}/prepared.pdb`, reference: `${prefix}/reference.sdf`,
    subset_remote_manifest: `${prefix}/manifest.json`, mapping: path.join(root, preparedReceptorV4.binding.path),
    quality_manifest: path.join(root, preparedReceptorV4.manifest.path), subset_manifest: path.join(root, manifest.scope.dataset_manifest_path) }
  data.environment.assets = { ...data.environment.assets, [binding.library]: subset.subset.sha256,
    [binding.receptor]: manifest.target.receptor_sha256, [binding.reference]: hash("SYNTHETIC reference; never submitted"),
    [binding.science_config]: manifest.recipe.sha256, [binding.subset_remote_manifest]: manifest.scope.dataset_manifest_sha256 }
  data.environment.research_inputs = { version: "screening-research-inputs-v1",
    library: { path: binding.library, sha256: subset.subset.sha256, records: 20000, selection: "frozen-file-order-prefix",
      source: { path: subset.source.path, sha256: subset.source.sha256 }, manifest: { path: binding.subset_remote_manifest, sha256: manifest.scope.dataset_manifest_sha256 } },
    receptor: { path: binding.receptor, sha256: manifest.target.receptor_sha256 },
    science_config: { path: binding.science_config, sha256: manifest.recipe.sha256 },
    reference: { path: binding.reference, sha256: data.environment.assets[binding.reference]! } }
  const prepared = await compileResearchExecution(data.contract, data.environment, binding)
  expect(prepared.plan.research_inputs?.receptor.sha256).toBe(manifest.target.receptor_sha256)
  expect(prepared.plan.max_records).toBe(20000)
  expect(prepared.plan.shortlist).toBe(5)
  expect(await verifyPreparedResearchExecution(prepared)).toEqual(prepared)
})

test("proof bytes require supported schemas and the registered geometry policy", async () => {
  const data = await fixture()
  data.contract.target.mapping_sha256 = hash("synthetic-test-proof")
  await writeFile(data.binding.mapping, "synthetic-test-proof")
  await expect(compileResearchExecution(data.contract, data.environment, data.binding)).rejects.toThrow()
  await sealProofs(data)
  data.proofs.quality.geometry.protein_vdw_ratio_minimum = 0.1
  await sealProofs(data)
  await expect(compileResearchExecution(data.contract, data.environment, data.binding)).rejects.toThrow()
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

test("host compilation binds target and hashes and reserves all CPU/GPU allocations at live rates", async () => {
  const data = await fixture()
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding)
  expect(prepared.plan).toMatchObject({ shortlist: 5, max_records: 20000, max_points: 0.6,
    target_parameters: { receptor: "1M17", chain: "A", residue_number: 797, reaction_type: "Michael Addition" } })
  expect(prepared.plan.finish_before - prepared.plan.created).toBe(data.contract.limits.max_elapsed_ms)
  expect(prepared.resources.gpu_hours).toBe(0.25)
  expect(prepared.resources.cpu_core_hours).toBeCloseTo(4 * 20 / 60 + 10 * 15 / 60)
  expect(prepared.resources.compute_points).toBeCloseTo(0.6)
  expect(researchExecutionResources(prepared.plan)).toEqual(prepared.resources)
  expect(prepared.plan_digest).toBe(digest(prepared.plan))
  expect(prepared.plan.origin).toEqual(data.environment.source.origin)
  const deadline = Date.now() + 60000
  expect((await compileResearchExecution(data.contract, data.environment, data.binding, { finish_before: deadline })).plan.finish_before).toBe(deadline)
  await expect(compileResearchExecution(data.contract, data.environment, data.binding, { finish_before: Date.now() - 1 }))
    .rejects.toThrow("deadline has expired")
})

test("contract modes, budgets, resource projects, source versions and proof hashes fail closed", async () => {
  const data = await fixture()
  await expect(compileResearchExecution({ ...data.contract, mode: "offline-replay" }, data.environment, data.binding)).rejects.toThrow("Native execution")
  const limited = structuredClone(data.contract)
  limited.limits.resources.cpu_core_hours = 2
  await expect(compileResearchExecution(limited, data.environment, data.binding)).rejects.toThrow("cpu_core_hours")
  const cheap = structuredClone(data.contract)
  cheap.limits.resources.compute_points = 0.59
  await expect(compileResearchExecution(cheap, data.environment, data.binding)).rejects.toThrow("compute_points")
  const wrong = structuredClone(data.environment)
  wrong.cpu.project = "other-project"
  await expect(compileResearchExecution(data.contract, wrong, data.binding)).rejects.toThrow("resource project")
  const release = structuredClone(data.contract)
  release.inputs.source_release = "7".repeat(64)
  await expect(compileResearchExecution(release, data.environment, data.binding)).rejects.toThrow("source release")
  const changed = structuredClone(data.contract)
  changed.inputs.library_sha256 = "8".repeat(64)
  await expect(compileResearchExecution(changed, data.environment, data.binding)).rejects.toThrow("input hash")
  await writeFile(data.binding.quality_manifest, "changed-proof")
  await expect(compileResearchExecution(data.contract, data.environment, data.binding)).rejects.toThrow("proof changed")
})

test("same durable binding survives concurrent calls and reopen without granting or submitting", async () => {
  const data = await fixture()
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding)
  const file = path.join(data.directory, "native.sqlite")
  const first = new Store(file)
  const second = new Store(file)
  try {
    expect(readResearchExecution(first, data.ref)).toBeNull()
    expect(first.db.query("SELECT name FROM sqlite_master WHERE name='research_task_bindings'").get()).toBeNull()
    const calls = await Promise.all([prepareResearchExecution(first, prepared, data.ref), prepareResearchExecution(second, prepared, data.ref)])
    expect(calls.filter(item => item.created)).toHaveLength(1)
    expect(calls[0]!.task.id).toBe(calls[1]!.task.id)
    expect(first.list()).toHaveLength(1)
    expect(first.get(prepared.plan.id)).toMatchObject({ state: "DRAFT", approval: null })
    expect(first.stages(prepared.plan.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
    const recompiled = await compileResearchExecution(data.contract, data.environment, data.binding)
    await expect(prepareResearchExecution(second, recompiled, data.ref)).rejects.toThrow("already binds")
  } finally { first.close(); second.close() }
  const reopened = new Store(file)
  try {
    const snapshot = readResearchExecution(reopened, data.ref)!
    expect(snapshot.task.id).toBe(prepared.plan.id)
    expect(snapshot.prepared).toEqual(prepared)
    expect(snapshot.usage).toBeNull()
    expect(snapshot.scientific_objective).toBe("not_evaluated_by_adapter")
    expect(snapshot.remote_artifacts_rehashed).toBe(false)
    expect((await prepareResearchExecution(reopened, prepared, data.ref)).created).toBe(false)
  } finally { reopened.close() }
})

test("native binding failure atomically rolls back the draft and its stages", async () => {
  const data = await fixture()
  const store = new Store(":memory:")
  try {
    const first = await compileResearchExecution(data.contract, data.environment, data.binding)
    await prepareResearchExecution(store, first, data.ref)
    store.db.exec("CREATE TRIGGER fail_research_binding BEFORE INSERT ON research_task_bindings BEGIN SELECT RAISE(ABORT, 'fixture interruption'); END")
    const next = await compileResearchExecution(data.contract, data.environment, data.binding)
    await expect(prepareResearchExecution(store, next, { ...data.ref, key: "round-2" })).rejects.toThrow("fixture interruption")
    expect(store.list()).toHaveLength(1)
    expect(store.stages(next.plan.id)).toHaveLength(0)
    expect(readResearchExecution(store, { ...data.ref, key: "round-2" })).toBeNull()
  } finally { store.close() }
})

test("proof changes after compilation cannot create a draft, and unrelated drafts are not adopted", async () => {
  const data = await fixture()
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding)
  const store = new Store(":memory:")
  try {
    await writeFile(data.binding.subset_manifest, "modified-subset-proof")
    await expect(prepareResearchExecution(store, prepared, data.ref)).rejects.toThrow("proof changed")
    expect(store.list()).toHaveLength(0)
    await writeFile(data.binding.subset_manifest, JSON.stringify(data.proofs.subset))
    store.create(prepared.plan)
    await expect(prepareResearchExecution(store, prepared, data.ref)).rejects.toThrow("without this research binding")
    expect(readResearchExecution(store, data.ref)).toBeNull()
    expect(store.list()).toHaveLength(1)
  } finally { store.close() }
})

test("native snapshot verifies complete manifest lineage while leaving scientific and billing claims to evaluators", async () => {
  const data = await fixture()
  const store = new Store(":memory:")
  try {
    const prepared = await compileResearchExecution(data.contract, data.environment, data.binding)
    const { task } = await prepareResearchExecution(store, prepared, data.ref)
    // Host-only synthetic state setup; no approval UI or remote computation is called.
    store.approve(task.id, task.digest, task.plan.session, task.plan.directory)
    const manifests: Outcome[] = []
    for (const stage of store.stages(task.id)) {
      const manifest = { task_id: task.id, phase: stage.phase, plan_digest: task.digest, status: "SUCCEEDED" as const,
        parent_digest: manifests.length ? digest(manifests.at(-1)) : null, input_count: 5, success_count: 5, failed_count: 0,
        identity_digest: "9".repeat(64), gates: { synthetic_orchestration_fixture: true }, runtime_seconds: 1,
        artifacts: { "synthetic.txt": hash("synthetic") } }
      manifests.push(manifest)
      store.stage(task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest) })
    }
    store.state(task.id, "SUCCEEDED")
    const snapshot = readResearchExecution(store, data.ref)!
    expect(snapshot.stages.map(stage => stage.manifest)).toEqual(manifests)
    expect(snapshot.usage).toBeNull()
    expect(snapshot.scientific_objective).toBe("not_evaluated_by_adapter")
    expect(() => readResearchExecution(store, { ...data.ref, contract_digest: "f".repeat(64) })).toThrow("another research")
    store.stage(task.id, 2, { manifest: JSON.stringify({ ...manifests[2], parent_digest: "0".repeat(64) }) })
    expect(() => readResearchExecution(store, data.ref)).toThrow("parent evidence")
  } finally { store.close() }
})
