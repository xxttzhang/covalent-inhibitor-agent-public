import { beforeEach, afterEach, afterAll, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import { createHash } from "node:crypto"
import { mkdirSync } from "node:fs"
import path from "node:path"
import { tmpdir } from "node:os"
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises"
import { Store } from "../inspire-screening/executor/store"
import { compileScreening } from "../inspire-screening/executor/compile"
import { estimate, policy } from "../inspire-screening/executor/resource-planner"
import { requestHash } from "../inspire-screening/executor/request"
import { digest, planSchema, type Outcome, type Phase } from "../inspire-screening/executor/model"
import { deliveryPath } from "../inspire-screening/executor/delivery"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, researchDigest, type ResearchContract } from "./research-loop-contract"
import { researchExecutionResources, type ResearchExecutionEnvironment } from "./research-execution"
import { readResearchNativeAction, nativeResearchCapability } from "./research-native-controller"
import { readResearchNativeReceipt } from "./research-native"
import { observeResearchNativeAction } from "./research-native-observation"
import { researchSnapshot } from "./research-loop"
import { evaluateResearchNativeAction, recoverResearchNativeEvaluation, researchScientificEvaluatorRegistry } from "./research-native-evaluation"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"

const root = process.env.COVSCREEN_ACCEPTANCE_ROOT ?? path.resolve(import.meta.dir, "../../../../..")
const scratch = await realpath(await mkdtemp(path.join(tmpdir(), "SYNTHETIC-native-scientific-evaluation-")))
afterAll(() => rm(scratch, { recursive: true, force: true }))
const hash = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex")
const registry = researchScientificEvaluatorRegistry["egfr-library-scientific-evaluator-v1"]
const historical = "reports/layout-migration-20261012/gpu-final"

// TEST ONLY: native tasks, selection/approval/publication/decision records are
// SYNTHETIC SQLite fixtures. Real historical candidate/PDB bytes and the frozen
// evaluator exercise real LOCAL CPU computation; no new native scientific run,
// model decision, remote execution or retrospective 20k provenance is claimed.
async function fixture(options: { unregistered?: boolean; missingPose?: boolean; budget?: boolean; v4?: boolean } = {}) {
  const registered = options.v4 ? researchScientificEvaluatorRegistry["egfr-library-scientific-evaluator-v4.0"] : registry
  const directory = await realpath(await mkdtemp(path.join(scratch, "case-")))
  async function copy(relative: string) {
    const bytes = await readFile(path.join(root, relative)), filename = path.join(directory, relative)
    await mkdir(path.dirname(filename), { recursive: true }); await writeFile(filename, bytes)
    return { path: filename, sha256: hash(bytes) }
  }
  const manifest = JSON.parse(await readFile(path.join(root, registered.manifest), "utf8"))
  const sourceAudit = await copy("reports/research-loop-20261013/research-scientific-receptor-audit-v1.json")
  for (const filename of [registered.manifest, registered.evaluator, manifest.target.receptor_path, manifest.recipe.path, manifest.scope.dataset_manifest_path]) await copy(filename)
  if (options.v4) {
    await copy(preparedReceptorV4.binding.path)
    const proof = JSON.parse(await readFile(path.join(root, preparedReceptorV4.binding.path), "utf8")) as {
      references: Record<string, { path: string; sha256: string }> }
    for (const item of Object.values(proof.references)) await copy(item.path)
    const receipt = JSON.parse(await readFile(path.join(root, proof.references.preparation_receipt!.path), "utf8")) as {
      artifacts: Record<string, string>; initial_inputs: Record<string, { path: string; sha256: string }>; initial_artifacts: Record<string, string>; parent_v3_artifacts: Record<string, string> }
    for (const name of Object.keys(receipt.artifacts)) await copy(path.join(path.dirname(proof.references.preparation_receipt!.path), name))
    for (const item of Object.values(receipt.initial_inputs)) await copy(item.path)
    for (const [role, artifacts] of [["receipt", receipt.initial_artifacts], ["parent_v3_receipt", receipt.parent_v3_artifacts]] as const)
      for (const name of Object.keys(artifacts)) await copy(path.join(path.dirname(receipt.initial_inputs[role]!.path), name))
  }
  await mkdir(path.join(directory, ".tooling")); await symlink(path.join(root, ".tooling/design-route-validation"), path.join(directory, ".tooling/design-route-validation"))
  const shared = "/inspire/fixture-shared", release = `/inspire/SYNTHETIC/releases/${"3".repeat(64)}`
  const binding = { library: `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv`,
    receptor: `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb`,
    science_config: `${release}/science/configs/covindb/EGFR_C797_mvp_v1.yaml`, mapping: options.v4 ? path.join(directory, preparedReceptorV4.binding.path) : sourceAudit.path,
    subset_manifest: path.join(directory, manifest.scope.dataset_manifest_path), quality_manifest: path.join(directory, registered.manifest) }
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "SYNTHETIC-native-evaluation-test", session: "SYNTHETIC-session",
    objective: "SYNTHETIC native DB with actual historical molecules for LOCAL evaluator test; not a new native research result", mode: "native-compute", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A", author_residue: 773, reactive_atom: "SG", warhead: "acrylamide",
      receptor_sha256: manifest.target.receptor_sha256, mapping_sha256: options.v4 ? preparedReceptorV4.binding.sha256 : sourceAudit.sha256 },
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", library_sha256: manifest.scope.dataset_source_sha256,
      subset_sha256: manifest.scope.dataset_manifest_sha256, source_release: "3".repeat(64), science_config_sha256: manifest.recipe.sha256 },
    quality: { manifest_sha256: registered.manifest_sha256, required_checks: ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility", "pose_geometry", "registered_objective_acceptance"],
      minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 8, compute_points: 2, model_tokens: 40000, model_cost: 1 }, max_library_records: 20000,
      max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10, max_gpu_concurrency: 1, max_gpu_hours_per_round: .25, max_decisions: 12, max_elapsed_ms: 7200000 },
    allowed_adaptations: ["inspect", "evaluate", "stop"] }
  if (options.unregistered) {
    const v2 = await copy("openscience-custom/backend/cli/integrations/discovery-platform/research-scientific-manifest-v2.json")
    contract.quality.manifest_sha256 = v2.sha256; binding.quality_manifest = v2.path
  }
  const request = "SYNTHETIC fixture only; no user-approved remote or model action"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: contract.project, group: "CPU", quota: "0,4,16", image: "fixture-image", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: .3 }
  const environment: ResearchExecutionEnvironment = { source: { text: request, origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant", part_ids: ["fixture-part"], request_sha256: requestHash(request) } },
    connection: { cli: "/nonexistent/SYNTHETIC-cli", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    runtime: { root: "/inspire/SYNTHETIC", project: `${release}/science`, worker: `${release}/worker.py`, activation: `${release}/science/environment/activate_carsidock.sh` },
    assets: { [binding.library]: contract.inputs.library_sha256!, [binding.receptor]: contract.target.receptor_sha256!, [binding.science_config]: contract.inputs.science_config_sha256!,
      [`${release}/worker.py`]: "5".repeat(64), [`${release}/manifest.json`]: "6".repeat(64) }, cpu,
    gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 2 } }
  const compiled = compileScreening({ ...environment, directory, session: contract.session,
    intent: { target: "EGFR", site: "C797", target_parameters: { target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
      insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" }, library: "CovInDB/Virtual_Screening_Library.csv", screening_mode: "research_pilot_v1",
      max_points: estimate(policy("research_pilot_v1"), { cpu: .3, gpu: 2 }) } })
  const plan = planSchema.parse({ ...compiled, finish_before: compiled.created + contract.limits.max_elapsed_ms, approve_before: Math.min(compiled.approve_before, compiled.created + contract.limits.max_elapsed_ms - 1) }), resources = researchExecutionResources(plan)
  if (options.budget) contract.limits.resources.cpu_core_hours = resources.cpu_core_hours + .001
  const prepared = { schema: "drug-design-research-execution-v1" as const, contract, contract_digest: researchDigest(contract), binding,
    plan, plan_digest: digest(plan), resources }
  const research = new ResearchLoopStore(path.join(directory, "research.sqlite")), native = new Store(path.join(directory, "native.sqlite"))
  const loop = research.create(contract, plan.created)
  // Direct fixture insertion is never actual scientific execution or approval.
  // V1 scientific audit remains FAIL; v4 has real preparation proof but the
  // task/delivery lineage below is SYNTHETIC and its poses are historical.
  research.record(loop.id, "native-plan:round-1", { schema: "drug-design-native-plan-offer-v1", loop_id: loop.id, key: "round-1", round: 1, prepared })
  const decision = research.decide(loop.id, research.get(loop.id).revision, { schema: "drug-design-recorded-proposal-v1", provenance: { kind: "model_response", fixture: "SYNTHETIC NOT A MODEL CALL" },
    decision: { schema: "drug-design-decision-v1", loop_id: loop.id, state_revision: research.get(loop.id).revision, action: "execute_prepared_action",
      capability_id: nativeResearchCapability, plan_id: plan.id, arguments: {}, evidence_refs: ["native-plan:round-1"], rationale: "SYNTHETIC fixture", alternatives: [], expected_observation: "SYNTHETIC", outcome: null, missing_inputs: [] } })
  research.record(loop.id, `guard-${decision.decision_id}`, { accepted: true, fixture: "SYNTHETIC fixture admission, not scientific or user approval" })
  research.reserve(loop.id, research.get(loop.id).revision, { key: "round-1", kind: "compute", round: 1, candidate_slots: 5, resources,
    plan_digest: digest(plan), source_release: contract.inputs.source_release })
  research.record(loop.id, `native-selection:${decision.decision_id}`, { schema: "drug-design-native-plan-selection-v1", decision_id: decision.decision_id,
    offer_ref: "native-plan:round-1", key: "round-1", task_id: plan.id, plan_digest: digest(plan) })
  native.create(plan); native.approve(plan.id, digest(plan), contract.session, directory)
  native.db.exec("CREATE TABLE research_task_bindings(loop TEXT NOT NULL,key TEXT NOT NULL,contract_digest TEXT NOT NULL,task_id TEXT NOT NULL UNIQUE,plan_digest TEXT NOT NULL,prepared TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(loop,key))")
  native.db.query("INSERT INTO research_task_bindings VALUES(?,?,?,?,?,?,?)").run(loop.id, "round-1", researchDigest(contract), plan.id, digest(plan), JSON.stringify(prepared), researchDigest(prepared))
  research.bindTask(loop.id, "round-1", plan.id, digest(plan)); research.claimSubmission(loop.id, "round-1", research.get(loop.id).revision, digest(plan))
  const candidateCsv = await readFile(path.join(root, historical, "prepare/stage2_candidates.csv"), "utf8")
  const candidateSdf = await readFile(path.join(root, historical, "prepare/stage2_candidates.sdf"), "utf8")
  const identities = candidateSdf.split("$$$$").filter(value => value.trim()).map(value => [
    Number(value.match(/>\s*<INPUT_SDF_INDEX>[^\n]*\n([^\n]+)/)![1]!.trim()),
    value.match(/>\s*<COMPOUND_ID>[^\n]*\n([^\n]+)/)![1]!.trim(),
  ]).sort((a, b) => Number(a[0]) - Number(b[0]))
  const selected = { schema_version: "1.0", status: "completed", selection_outcome: "CANDIDATES_SELECTED", synthetic_fixture: "SYNTHETIC selection provenance; actual historical output is not assigned to today's prefix",
    source: { path: binding.library, sha256: contract.inputs.library_sha256, max_rows: 20000, records_read: 20000, record_selection: "file_order_prefix" },
    target: { config_path: binding.science_config, config_sha256: contract.inputs.science_config_sha256, target_id: "EGFR", site_id: "EGFR_C797" },
    selection: { top_n_requested: 5, top_n_selected: 5 }, counts: { input_rows: 20000, selected: 5 }, quality_gates: { synthetic_identity_gate: true } }
  const files: Record<string, Record<string, string>> = { prepare: { "stage2_selection_manifest.json": JSON.stringify(selected), "stage2_candidates.csv": candidateCsv,
    "stage2_candidates.sdf": await readFile(path.join(root, historical, "prepare/stage2_candidates.sdf"), "utf8") },
    carsidock: { "scores.csv": await readFile(path.join(root, historical, "carsidock/scores.csv"), "utf8"), "carsidock.sdf": await readFile(path.join(root, historical, "carsidock/carsidock.sdf"), "utf8") },
    rtmscore: { "scores.csv": await readFile(path.join(root, historical, "rtmscore/scores.csv"), "utf8") },
    report: { "candidates.csv": await readFile(path.join(root, historical, "migration-candidates.csv"), "utf8") } }
  const outcomes: Outcome[] = []
  for (const stage of native.stages(plan.id)) {
    const content = files[stage.phase]!
    const outcome: Outcome = { task_id: plan.id, phase: stage.phase, plan_digest: digest(plan), status: "SUCCEEDED", parent_digest: outcomes.length ? digest(outcomes.at(-1)) : null,
      input_count: 5, success_count: 5, failed_count: 0, identity_digest: hash(JSON.stringify(identities)), gates: { synthetic_control_fixture_only: true }, runtime_seconds: 1,
      artifacts: Object.fromEntries(Object.entries(content).map(([name, text]) => [name, hash(text)])) }
    outcomes.push(outcome); native.stage(plan.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(outcome), submitted: 1000 })
    for (const [name, text] of Object.entries(content)) {
      if (options.missingPose && name === "carsidock.sdf") continue
      const file = await deliveryPath(native.get(plan.id), { phase: stage.phase as Phase, name, sha256: hash(text) })
      await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, text)
      native.db.query("INSERT INTO artifact_publications VALUES(?,?,?,?,?)").run(plan.id, stage.phase, name, digest(plan), JSON.stringify({ phase: stage.phase, name,
        artifact_id: "art_" + hash("SYNTHETIC-" + name).slice(0,12), version_id: "ver_" + hash("SYNTHETIC-" + name).slice(0,12), project_id: contract.project,
        sha256: hash(text), bytes: Buffer.byteLength(text), published_at: 1000, evidence_kind: "core_result" }))
    }
  }
  native.state(plan.id, "SUCCEEDED")
  research.transition(loop.id, research.get(loop.id).revision, "RUNNING", { fixture: "SYNTHETIC native results; no remote task actually ran" })
  return { directory, research, native, id: loop.id, contract, prepared, close: () => { research.close(); native.close() } }
}

test("SYNTHETIC native lineage evaluates actual v4 preparation and preserves measured CPU on reopen without rerunning", async () => {
  const data = await fixture({ v4: true })
  try {
    const value = await evaluateResearchNativeAction(data)
    if (value.status !== "completed") throw Error(value.reason)
    expect(value.observation.integrity_errors).toEqual([])
    expect(value.observation.scientific_checks.find(item => item.id === "target_construct_and_residue_mapping")?.status).toBe("pass")
    expect(value.observation.scientific_checks.every(item => item.evaluator === "egfr-library-scientific-evaluator-v4.0")).toBe(true)
    expect(value.observation.counts.selected).toBe(5)
    const process = data.research.evidence(data.id, "host").find(item => item.key.startsWith("evaluation-process:"))!
    const receipt = process.payload as { exit_code: number; output: { path: string }; cpu_user_microseconds: number; cpu_system_microseconds: number }
    expect(receipt.exit_code).toBe(0)
    const output = JSON.parse(await readFile(receipt.output.path, "utf8"))
    expect(output.receptor.status).toBe("pass")
    expect(output.native_execution_provenance_verified).toBe(false)
    expect(output.candidates).toHaveLength(5)
    expect(value.observation.counts.scientific_passed).toBe(output.counts.pass)
    const reservation = data.research.reservations(data.id).find(item => item.payload.kind === "evaluation")!
    expect(reservation.actual?.cpu_core_hours).toBe((receipt.cpu_user_microseconds + receipt.cpu_system_microseconds) / 3_600_000_000)
    expect(data.research.reservations(data.id).find(item => item.payload.kind === "compute")!.actual).toBeNull()
    data.research.close()
    const reopened = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
    try {
      const recovered = await recoverResearchNativeEvaluation({ ...data, research: reopened })
      expect(recovered.status).toBe("completed")
      expect(recovered.observation?.id).toBe(value.observation.id)
      expect(reopened.evidence(data.id, "host").filter(item => item.key.startsWith("evaluation-process:"))).toHaveLength(1)
    } finally { reopened.close() }
  } finally { data.close() }
})

test("SYNTHETIC native lineage runs the actual frozen evaluator on real historical bytes, records CPU, and keeps science unknown", async () => {
  const data = await fixture()
  try {
    const value = await evaluateResearchNativeAction(data)
    if (value.status !== "completed") throw Error(value.reason)
    expect(value.status).toBe("completed")
    expect(value.observation).toMatchObject({ classification: "incomplete", counts: { selected: 5, scientific_passed: 0 } })
    expect(value.observation.candidates.every(candidate => candidate.scientific_status === "unknown")).toBe(true)
    expect(value.observation.scientific_checks.find(check => check.id === "warhead_and_reaction_compatibility")?.status).toBe("pass")
    expect(value.observation.scientific_checks.find(check => check.id === "registered_objective_acceptance")?.status).toBe("unknown")
    const reserved = data.research.reservations(data.id)
    expect(reserved.filter(row => row.payload.kind === "compute")).toHaveLength(1)
    const evaluation = reserved.find(row => row.payload.kind === "evaluation")!
    expect(evaluation.state).toBe("SETTLED")
    expect(evaluation.actual!.cpu_core_hours).toBeGreaterThan(0)
    expect(evaluation.actual!.cpu_core_hours).toBeLessThan(60 / 3600)
    expect(evaluation.actual!.gpu_hours).toBe(0)
    expect(reserved.find(row => row.payload.kind === "compute")!.state).toBe("RESERVED")
    const receipts = data.research.evidence(data.id, "host").filter(row => row.key.startsWith("evaluation-process:"))
    expect(receipts).toHaveLength(1)
    expect(receipts[0]!.payload).toMatchObject({ usage_origin: "host_Bun_Subprocess_resourceUsage_wait4", exit_code: 0 })
    expect((await evaluateResearchNativeAction(data)).observation).toEqual(value.observation)
    expect((await recoverResearchNativeEvaluation(data)).observation).toEqual(value.observation)
    expect(data.research.evidence(data.id, "host").filter(row => row.key.startsWith("evaluation-process:"))).toEqual(receipts)
    const sent = JSON.parse(researchSnapshot(data.research, data.id).prompt.user)
    expect(sent.observations.find((item: {id:string}) => item.id === value.observation.id).scientific_checks).toEqual(value.observation.scientific_checks)
    expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
  } finally { data.close() }
})

test.each([
  ["unregistered", "Scientific evaluator/manifest version is not registered"],
  ["missingPose", "Native result identity, denominator or delivered artifact integrity is incomplete"],
  ["budget", "Cumulative research budget exceeded: cpu_core_hours"],
] as const)("%s is rejected before a scientific process receipt", async (kind, reason) => {
  const data = await fixture({ [kind]: true })
  try {
    await expect(evaluateResearchNativeAction(data)).rejects.toThrow(reason)
    expect(data.research.evidence(data.id, "host").some(row => row.key.startsWith("evaluation-process:"))).toBe(false)
    expect(data.research.reservations(data.id).filter(row => row.payload.kind === "evaluation")).toHaveLength(0)
  } finally { data.close() }
})

test("a reopened host restores the completed observation without another evaluator process and rejects changed output bytes", async () => {
  const data = await fixture()
  const result = await evaluateResearchNativeAction(data)
  if (result.status !== "completed") { data.close(); throw Error(result.reason) }
  data.close()
  const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite")), native = new Store(path.join(data.directory, "native.sqlite"))
  const input = { research, native, id: data.id }
  try {
    const before = research.reservations(data.id)
    const recovered = await recoverResearchNativeEvaluation(input)
    expect(recovered.observation).toEqual(result.observation)
    expect(research.reservations(data.id)).toEqual(before)
    const receipts = research.evidence(data.id, "host").filter(row => row.key.startsWith("evaluation-process:"))
    expect(receipts).toHaveLength(1)
    const output = (receipts[0]!.payload as {output: {path: string}}).output.path
    await writeFile(output, JSON.stringify({ origin: "native_execution", status: "pass", candidate_count: 5 }))
    await expect(recoverResearchNativeEvaluation(input)).rejects.toThrow("Scientific evidence bytes changed")
    await expect(evaluateResearchNativeAction(input)).rejects.toThrow("Scientific evidence bytes changed")
    expect(research.evidence(data.id, "host").filter(row => row.key.startsWith("evaluation-process:"))).toEqual(receipts)
    expect(research.reservations(data.id)).toEqual(before)
  } finally { research.close(); native.close() }
})

test("changed evaluator bytes and a pre-aborted request cannot start or reserve local CPU", async () => {
  const data = await fixture()
  try {
    const abort = new AbortController(); abort.abort(Error("Host request was cancelled"))
    await expect(evaluateResearchNativeAction({ ...data, abort: abort.signal })).rejects.toThrow("Host request was cancelled")
    await writeFile(path.join(data.directory, registry.evaluator), "raise RuntimeError('SYNTHETIC tampered program must never execute')\n")
    await expect(evaluateResearchNativeAction(data)).rejects.toThrow("Scientific evidence bytes changed")
    expect(data.research.reservations(data.id).filter(row => row.payload.kind === "evaluation")).toHaveLength(0)
    expect(data.research.evidence(data.id, "host").some(row => row.key.startsWith("evaluation-process:"))).toBe(false)
  } finally { data.close() }
})

test("a real bounded subprocess import failure records actual CPU and an execution failure without changing native engine success", async () => {
  const data = await fixture()
  try {
    // Isolated TEST environment intentionally lacks third-party packages. The
    // interpreter and evaluator are still the exact registered binary/bytes.
    const environment = path.join(data.directory, ".tooling/design-route-validation")
    await rm(environment)
    await mkdir(path.join(environment, "bin"), { recursive: true })
    await symlink(await realpath(path.join(root, registry.interpreter)), path.join(environment, "bin/python"))
    await writeFile(path.join(environment, "pyvenv.cfg"), await readFile(path.join(root, ".tooling/design-route-validation/pyvenv.cfg")))
    const result = await evaluateResearchNativeAction(data)
    if (result.status !== "completed") throw Error(result.reason)
    expect(result.observation).toMatchObject({ classification: "execution_failure", failure: { category: "environment" }, run: { status: "succeeded" } })
    expect(result.observation.scientific_checks.every(check => check.status === "unknown")).toBe(true)
    const reservation = data.research.reservations(data.id).find(row => row.payload.kind === "evaluation")!
    expect(reservation.state).toBe("SETTLED")
    expect(reservation.actual!.cpu_core_hours).toBeGreaterThan(0)
    const receipt = data.research.evidence(data.id, "host").find(row => row.key.startsWith("evaluation-process:"))!.payload as {exit_code:number;output:null;stderr:{path:string}}
    expect(receipt.exit_code).not.toBe(0)
    expect(receipt.output).toBeNull()
    expect(await readFile(receipt.stderr.path, "utf8")).toContain("ModuleNotFoundError")
    expect((await recoverResearchNativeEvaluation(data)).observation).toEqual(result.observation)
  } finally { data.close() }
})

test.each(["log_io", "process_receipt_commit"] as const)("known wait4 CPU survives %s failure without repeating the actual evaluator", async failure => {
  const data = await fixture()
  try {
    const original = data.research.record.bind(data.research)
    data.research.record = (id, key, payload, now) => {
      if (failure === "process_receipt_commit" && key.startsWith("evaluation-process:")) throw Error("SYNTHETIC result receipt database interruption")
      const value = original(id, key, payload, now)
      if (failure === "log_io" && key.startsWith("evaluation-resource:")) {
        const attempt = data.research.evidence(id, "host").find(row => row.key.startsWith("evaluation-attempt:"))!.payload as { directory: string }
        // Deterministic filesystem fault AFTER real wait4 CPU has been recorded.
        mkdirSync(path.join(attempt.directory, "stdout.txt"))
      }
      return value
    }
    const result = await evaluateResearchNativeAction(data)
    const evidence = data.research.evidence(data.id, "host")
    const usage = evidence.find(row => row.key.startsWith("evaluation-resource:"))!.payload as { cpu_user_microseconds:number;cpu_system_microseconds:number }
    expect(usage.cpu_user_microseconds + usage.cpu_system_microseconds).toBeGreaterThan(0)
    const reservation = data.research.reservations(data.id).find(row => row.payload.kind === "evaluation")!
    if (failure === "log_io") {
      expect(result.status).toBe("completed")
      expect(result.observation?.classification).toBe("integrity_failure")
      expect(result.observation?.scientific_checks.every(check => check.status === "unknown")).toBe(true)
      expect(reservation.state).toBe("SETTLED")
      expect(reservation.actual?.cpu_core_hours).toBe((usage.cpu_user_microseconds + usage.cpu_system_microseconds) / 3_600_000_000)
      const receipt = evidence.find(row => row.key.startsWith("evaluation-process:"))!.payload
      expect(receipt).toMatchObject({ stdout: null })
      expect((await recoverResearchNativeEvaluation(data)).observation).toEqual(result.observation)
    } else {
      expect(result.status).toBe("needs_reconciliation")
      expect(reservation.state).toBe("RESERVED")
      const recovered = await recoverResearchNativeEvaluation(data)
      expect(recovered.status).toBe("needs_reconciliation")
      if (recovered.status === "needs_reconciliation") expect(recovered.reason).toContain("Host CPU receipt is retained")
      expect((await evaluateResearchNativeAction(data)).status).toBe("needs_reconciliation")
    }
    expect(data.research.evidence(data.id, "host").filter(row => row.key.startsWith("evaluation-resource:"))).toHaveLength(1)
    expect(data.research.reservations(data.id).find(row => row.payload.kind === "evaluation")).toEqual(reservation)
  } finally { data.close() }
})

test("a crash after durable claim retains CPU reservation and recovery never retries the child", async () => {
  const data = await fixture()
  try {
    await observeResearchNativeAction(data)
    const native = await readResearchNativeReceipt({ research: data.research, native: data.native, reference: readResearchNativeAction(data).reference })
    const { observed_at: _, ...content } = native
    const key = "evaluation-" + researchDigest({ task: native.task_id, plan: native.plan_digest, receipt: researchDigest(content), registry })
    await mkdir(path.join(data.directory, ".research-evaluations", data.id, key), { recursive: true })
    expect((await evaluateResearchNativeAction(data)).status).toBe("needs_reconciliation")
    const before = data.research.reservations(data.id)
    expect(before.find(row => row.payload.kind === "evaluation")?.state).toBe("RESERVED")
    expect((await recoverResearchNativeEvaluation(data)).status).toBe("needs_reconciliation")
    expect((await evaluateResearchNativeAction(data)).status).toBe("needs_reconciliation")
    expect(data.research.reservations(data.id)).toEqual(before)
    expect(data.research.evidence(data.id, "host").some(row => row.key.startsWith("evaluation-process:"))).toBe(false)
  } finally { data.close() }
})
