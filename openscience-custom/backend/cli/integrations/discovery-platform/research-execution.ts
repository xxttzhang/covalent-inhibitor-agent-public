import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, readFile, realpath } from "node:fs/promises"
import { compileScreening } from "../inspire-screening/executor/compile"
import { digest, planSchema, type Plan } from "../inspire-screening/executor/model"
import { estimate, policy } from "../inspire-screening/executor/resource-planner"
import type { Store } from "../inspire-screening/executor/store"
import { verifiedExecution } from "../inspire-screening/executor/execution-evidence"
import { researchContract, researchDigest, researchHash, researchResources, resourceKeys, exceedsResearchLimit,
  type ResearchContract } from "./research-loop-contract"
import { verifyPreparedReceptorEvidence } from "./research-prepared-receptor"

const remote = z.string().startsWith("/inspire/")
const local = z.string().startsWith("/")
export const researchExecutionBinding = z.object({
  library: remote, receptor: remote, science_config: remote,
  reference: remote.optional(), subset_remote_manifest: remote.optional(),
  mapping: local, subset_manifest: local, quality_manifest: local,
}).strict()
export type ResearchExecutionBinding = z.infer<typeof researchExecutionBinding>
export type ResearchExecutionEnvironment = Omit<Parameters<typeof compileScreening>[0], "intent" | "directory" | "session">

// This adapter currently has one worker whose input roles are fixed in code.
// An unrelated extra entry in plan.assets must never stand in for those inputs.
const status = z.enum(["pass", "fail", "unknown"])
const receptorAudit = z.object({
  schema: z.literal("drug-design-receptor-audit-v1"), evaluator: z.literal("egfr-library-scientific-evaluator-v1"),
  evaluator_sha256: researchHash, structure_sha256: researchHash, entry_id: z.string(), uniprot: z.string(),
  uniprot_residue: z.number().int(), auth_asym_id: z.string(), author_residue_id: z.string(), reactive_atom: z.string(),
  taxon: z.number().int(), mapping_method: z.literal("frozen_PDB_DBREF_and_SEQADV_plus_coordinate_audit"),
  mapping_status: status, status,
  checks: z.array(z.object({ id: z.string(), status, detail: z.string(), measurements: z.record(z.string(), z.unknown()) }).strict()),
  construct_scope: z.string().min(1), new_compute: z.literal(false),
}).strict()
const frozenSubset = z.object({
  schema: z.literal("drug-design-frozen-library-prefix-v1"), freezer_sha256: researchHash,
  operation: z.literal("data-freeze-only-no-molecule-screening"), header: z.array(z.string()).min(1),
  selection: z.object({ duplicates_preserved: z.literal(true), method: z.literal("file_order_prefix"),
    records: z.number().int().positive(), source_record_index_base: z.literal(0), unique_row_hashes: z.number().int().positive() }).strict(),
  source: z.object({ path: remote, sha256: researchHash, size_bytes: z.number().int().positive() }).strict(),
  subset: z.object({ encoding: z.literal("utf-8"), filename: z.literal("library-prefix.csv"), line_ending: z.literal("LF"),
    semantics: z.string().min(1), sha256: researchHash, size_bytes: z.number().int().positive() }).strict(),
  records: z.array(z.object({ row_sha256: researchHash, source_record_index: z.number().int().nonnegative() }).strict()).max(20000),
}).strict()
const scientificManifestV1 = z.object({
  schema: z.literal("drug-design-scientific-manifest-v1"), version: z.literal("egfr-c797-library-computational-shortlist-v1"),
  scope: z.object({ conclusion: z.literal("computational-shortlist-only"), max_candidates: z.number().int().positive(),
    minimum_accepted_candidates: z.number().int().positive(), source_rows: z.number().int().positive(), selection: z.literal("file_order_prefix"),
    dataset_subset_sha256: researchHash, dataset_source_sha256: researchHash, dataset_manifest_sha256: researchHash,
    dataset_manifest_path: z.string().min(1), dataset_note: z.string().min(1) }).strict(),
  evaluator: z.object({ version: z.literal("egfr-library-scientific-evaluator-v1"), path: z.string().min(1), sha256: researchHash,
    packages: z.object({ rdkit: z.string().min(1), numpy: z.string().min(1), pyyaml: z.string().min(1) }).strict() }).strict(),
  target: z.object({ name: z.string(), uniprot: z.string(), entry: z.string(), chain: z.string(),
    author_residue: z.number().int(), canonical_residue: z.number().int(), residue: z.string(), atom: z.string(),
    construct: z.string().min(1), receptor_path: z.string().min(1), receptor_sha256: researchHash, coordinate_acceptance: z.string().min(1) }).strict(),
  recipe: z.object({ path: z.string().min(1), sha256: researchHash, reaction: z.literal("Michael Addition"), warhead: z.literal("acrylamide") }).strict(),
  artifacts: z.object({ candidates_csv: z.literal("prepare/stage2_candidates.csv"), candidates_sdf: z.literal("prepare/stage2_candidates.sdf"),
    docking_csv: z.literal("carsidock/scores.csv"), poses_sdf: z.literal("carsidock/carsidock.sdf"), scoring_csv: z.literal("rtmscore/scores.csv") }).strict(),
  geometry: z.object({ anchor_coordinate_tolerance_angstrom: z.literal(0.001), bond_relative_tolerance: z.literal(0.25),
    angle_relative_tolerance: z.literal(0.25), internal_clash_lower_tolerance: z.literal(0.3),
    aromatic_plane_tolerance_angstrom: z.literal(0.25), protein_vdw_ratio_minimum: z.literal(0.75) }).strict(),
  acceptance: z.object({ all_candidate_checks_must_pass: z.literal(true), minimum_accepted_candidates_rationale: z.string().min(1),
    engine_scores: z.string().min(1), failure_policy: z.string().min(1), retained_stereochemistry: z.string().min(1) }).strict(),
  provenance: z.array(z.object({ source: z.string(), use: z.string() }).strict()), adaptations: z.array(z.string()), limitations: z.array(z.string()),
}).strict()
const requiredQualityChecks = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility",
  "pose_geometry", "registered_objective_acceptance"]
const scientificManifestV2 = scientificManifestV1.extend({
  schema: z.literal("drug-design-scientific-manifest-v2"),
  version: z.literal("egfr-c797-library-computational-shortlist-v2.1"),
  target: scientificManifestV1.shape.target.extend({ mapping_path: z.string().min(1), mapping_sha256: researchHash }),
  evaluator: scientificManifestV1.shape.evaluator.extend({ version: z.literal("egfr-library-scientific-evaluator-v2.1") }),
  required_scientific_checks: z.array(z.string().min(1)), required_receptor_checks: z.array(z.string().min(1)),
  prepared_receptor_semantics: z.object({ method_revision: z.literal("2.1"), thresholds_changed_from_v1: z.literal(false),
    candidate_chemistry_and_geometry_core_sha256: researchHash, failure_policy: z.string().min(1) }).strict(),
})
const scientificManifestV4 = scientificManifestV2.extend({
  schema: z.literal("drug-design-scientific-manifest-v4"), version: z.literal("egfr-c797-library-computational-shortlist-v4.0"),
  evaluator: scientificManifestV1.shape.evaluator.extend({ version: z.literal("egfr-library-scientific-evaluator-v4.0") }),
  prepared_receptor_semantics: scientificManifestV2.shape.prepared_receptor_semantics.extend({ method_revision: z.literal("4.0") }),
})
const scientificManifest = z.discriminatedUnion("schema", [scientificManifestV1, scientificManifestV2, scientificManifestV4])

export const preparedResearchExecution = z.object({
  schema: z.literal("drug-design-research-execution-v1"), contract: researchContract,
  contract_digest: researchHash, binding: researchExecutionBinding,
  plan: planSchema, plan_digest: researchHash, resources: researchResources,
}).strict()
export type PreparedResearchExecution = z.infer<typeof preparedResearchExecution>

/** Concrete allocations, including CPU cores allocated alongside the GPU.
 * These are ceilings derived from the frozen plan, never measured usage or bills. */
export function researchExecutionResources(value: Plan) {
  const plan = planSchema.parse(value)
  return researchResources.parse(plan.steps.reduce((total, step) => {
    const [gpus, cpus] = step.resource.quota.split(",").map(Number)
    return { ...total, gpu_hours: total.gpu_hours + gpus! * step.minutes / 60,
      cpu_core_hours: total.cpu_core_hours + cpus! * step.minutes / 60,
      compute_points: total.compute_points + step.resource.points_per_hour * step.minutes / 60 }
  }, { gpu_hours: 0, cpu_core_hours: 0, compute_points: 0, model_tokens: 0, model_cost: 0 }))
}

function validate(prepared: PreparedResearchExecution) {
  const contract = prepared.contract
  const plan = prepared.plan
  if (prepared.contract_digest !== researchDigest(contract) || prepared.plan_digest !== digest(plan))
    throw Error("Research contract or exact native plan digest changed")
  if (contract.mode !== "native-compute" || plan.directory !== contract.directory || plan.session !== contract.session)
    throw Error("Native execution must belong to its research contract conversation and directory")
  if (plan.steps.some(step => step.resource.project !== contract.project))
    throw Error("Native resource project differs from the research contract")
  if (plan.finish_before > plan.created + contract.limits.max_elapsed_ms)
    throw Error("Native task deadline exceeds research elapsed-time limit")
  if (contract.inputs.library !== plan.library || contract.limits.max_candidates_per_round !== 5 ||
    plan.max_records !== 20000 || plan.shortlist !== 5 || plan.automation?.selected_mode !== "research_pilot_v1" ||
    plan.automation.requested_mode !== "research_pilot_v1" || !plan.origin)
    throw Error("Research execution differs from the registered 20k/5 pilot or lacks bound request provenance")
  if (plan.target !== contract.target.gene || plan.site !== contract.target.canonical_site ||
    plan.target_parameters?.receptor !== contract.target.pdb || plan.target_parameters.chain !== contract.target.chain ||
    plan.target_parameters.residue !== "CYS" || plan.target_parameters.residue_number !== 797 ||
    plan.target_parameters.numbering !== "canonical" || plan.target_parameters.insertion_code !== "" ||
    plan.target_parameters.reaction_type !== "Michael Addition")
    throw Error("Research target, receptor or reaction differs from the native plan")
  const release = path.posix.dirname(plan.runtime.worker)
  if (plan.runtime.worker !== `${release}/worker.py` || release !== `${plan.runtime.root}/releases/${contract.inputs.source_release}` ||
    plan.runtime.project !== `${release}/science` || plan.runtime.activation !== `${release}/science/environment/activate_carsidock.sh` ||
    !plan.assets[`${release}/manifest.json`] || !plan.assets[plan.runtime.worker])
    throw Error("Native worker and science sources must use the contract's frozen source release")
  const explicit = plan.research_inputs
  if (explicit) {
    if (prepared.binding.library !== explicit.library.path || prepared.binding.receptor !== explicit.receptor.path ||
      prepared.binding.science_config !== explicit.science_config.path || prepared.binding.reference !== explicit.reference.path ||
      prepared.binding.subset_remote_manifest !== explicit.library.manifest.path ||
      explicit.library.source.sha256 !== contract.inputs.library_sha256 ||
      explicit.library.manifest.sha256 !== contract.inputs.subset_sha256)
      throw Error("Research input roles or original-library provenance differ from the explicit worker binding")
  } else {
    const shared = process.env.COVSCREEN_LEGACY_SHARED_ROOT
    if (!shared || !remote.safeParse(shared).success || path.posix.normalize(shared) !== shared)
      throw Error("Configure COVSCREEN_LEGACY_SHARED_ROOT for historical worker input roles")
    if (prepared.binding.library !== `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv` ||
      prepared.binding.receptor !== `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb` ||
      prepared.binding.science_config !== `${plan.runtime.project}/configs/covindb/EGFR_C797_mvp_v1.yaml` ||
      prepared.binding.reference !== undefined || prepared.binding.subset_remote_manifest !== undefined)
      throw Error("Research input paths differ from the registered worker's consumed input roles")
  }
  for (const [filename, expected] of [[prepared.binding.library, explicit?.library.sha256 ?? contract.inputs.library_sha256],
    [prepared.binding.receptor, contract.target.receptor_sha256],
    [prepared.binding.science_config, contract.inputs.science_config_sha256]])
    if (plan.assets[filename!] !== expected) throw Error("Research input hash differs from the frozen native assets")
  const resources = researchExecutionResources(plan)
  if (researchDigest(resources) !== researchDigest(prepared.resources)) throw Error("Research resource reservation differs from the native allocations")
  for (const key of resourceKeys)
    if (exceedsResearchLimit(resources[key], contract.limits.resources[key])) throw Error("Native plan exceeds research budget: " + key)
  if (resources.gpu_hours > contract.limits.max_gpu_hours_per_round + 1e-9 ||
    plan.max_points === null || Math.abs(plan.max_points - resources.compute_points) > 1e-8)
    throw Error("Native plan must reserve its exact bounded GPU and live-rate point cost")
  return prepared
}

async function verifyProofs(prepared: PreparedResearchExecution) {
  const root = await realpath(prepared.contract.directory)
  const proofs = [[prepared.binding.mapping, prepared.contract.target.mapping_sha256],
    [prepared.binding.subset_manifest, prepared.contract.inputs.subset_sha256],
    [prepared.binding.quality_manifest, prepared.contract.quality.manifest_sha256]] as const
  const contents: unknown[] = []
  for (const [filename, expected] of proofs) {
    const actual = await realpath(filename)
    if (actual !== filename || !actual.startsWith(root + path.sep)) throw Error("Research proof must be a contained regular file without symlink substitution")
    const stat = await lstat(actual)
    if (!stat.isFile() || stat.size > 16 * 1024 * 1024) throw Error("Research proof is not a bounded regular file")
    const bytes = await readFile(actual)
    if (createHash("sha256").update(bytes).digest("hex") !== expected)
      throw Error("Research input/quality proof changed")
    contents.push(JSON.parse(bytes.toString("utf8")))
  }
  const subset = frozenSubset.parse(contents[1])
  const quality = scientificManifest.parse(contents[2])
  const contract = prepared.contract
  const audited = quality.schema !== "drug-design-scientific-manifest-v1" ? await (async () => {
    if (quality.target.mapping_sha256 !== contract.target.mapping_sha256 ||
      path.resolve(root, quality.target.mapping_path) !== prepared.binding.mapping ||
      quality.required_scientific_checks.length !== requiredQualityChecks.length ||
      new Set(quality.required_scientific_checks).size !== requiredQualityChecks.length ||
      requiredQualityChecks.some(id => !quality.required_scientific_checks.includes(id)))
      throw Error("Prepared receptor binding or scientific check registry differs from the contract")
    return verifyPreparedReceptorEvidence({ root, value: contents[0], contract, geometry: quality.geometry,
      evaluator: quality.evaluator, receptor_path: quality.target.receptor_path, required_checks: quality.required_receptor_checks,
      core_sha256: quality.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 })
  })() : (() => {
    const audit = receptorAudit.parse(contents[0])
    const checks = new Set(audit.checks.map(item => item.id))
    if (audit.mapping_status !== "pass" || audit.status !== "pass" || audit.checks.some(item => item.status !== "pass") ||
      checks.size !== audit.checks.length || ["receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom", "protein_coordinates"].some(id => !checks.has(id)))
      throw Error("Receptor and mapping audit must pass every required check before native preparation")
    if (audit.structure_sha256 !== contract.target.receptor_sha256 || audit.entry_id !== contract.target.pdb ||
      audit.uniprot !== contract.target.uniprot || audit.uniprot_residue !== 797 || audit.auth_asym_id !== contract.target.chain ||
      audit.author_residue_id !== String(contract.target.author_residue) || audit.reactive_atom !== contract.target.reactive_atom ||
      audit.taxon !== contract.target.taxon || audit.checks.find(item => item.id === "receptor_bytes")?.measurements.sha256 !== audit.structure_sha256)
      throw Error("Receptor audit does not bind the contract's exact receptor, species and residue identity")
    return { evaluator_sha256: audit.evaluator_sha256 }
  })()
  const target = quality.target
  if (target.name !== contract.target.gene || target.uniprot !== contract.target.uniprot || target.entry !== contract.target.pdb ||
    target.chain !== contract.target.chain || target.author_residue !== contract.target.author_residue || target.canonical_residue !== 797 ||
    target.residue !== "CYS" || target.atom !== contract.target.reactive_atom || target.receptor_sha256 !== contract.target.receptor_sha256 ||
    quality.recipe.sha256 !== contract.inputs.science_config_sha256 || quality.recipe.warhead !== contract.target.warhead ||
    quality.evaluator.sha256 !== audited.evaluator_sha256)
    throw Error("Scientific quality policy differs from the audited receptor, recipe or evaluator")
  if (contract.quality.required_checks.length !== requiredQualityChecks.length ||
    requiredQualityChecks.some(id => !contract.quality.required_checks.includes(id)) ||
    quality.scope.conclusion !== contract.quality.conclusion || quality.scope.minimum_accepted_candidates !== contract.quality.minimum_candidates ||
    quality.scope.max_candidates !== contract.limits.max_candidates_per_round)
    throw Error("Research quality requirements differ from the registered scientific evaluator")
  const source = prepared.plan.research_inputs?.library.source.path ?? prepared.binding.library
  const explicit = prepared.plan.research_inputs
  if (explicit && (explicit.library.sha256 !== subset.subset.sha256 || explicit.library.records !== subset.selection.records ||
    explicit.library.manifest.sha256 !== contract.inputs.subset_sha256))
    throw Error("Explicit worker CSV differs from the frozen subset bytes, count or manifest")
  if (subset.source.path !== source || subset.source.sha256 !== contract.inputs.library_sha256 ||
    subset.selection.records !== prepared.plan.max_records || subset.records.length !== prepared.plan.max_records ||
    subset.records.some((item, index) => item.source_record_index !== index) ||
    new Set(subset.records.map(item => item.row_sha256)).size !== subset.selection.unique_row_hashes ||
    quality.scope.source_rows !== subset.selection.records || quality.scope.dataset_source_sha256 !== subset.source.sha256 ||
    quality.scope.dataset_subset_sha256 !== subset.subset.sha256 || quality.scope.dataset_manifest_sha256 !== contract.inputs.subset_sha256 ||
    path.resolve(root, quality.scope.dataset_manifest_path) !== prepared.binding.subset_manifest)
    throw Error("Frozen subset source, row order/count or file identity differs from the native source prefix and quality policy")
}

/** Revalidate a host-prepared exact plan without creating a task, reserving
 * resources, requesting permission, or sending a network request. */
export async function verifyPreparedResearchExecution(input: unknown): Promise<PreparedResearchExecution> {
  const prepared = validate(preparedResearchExecution.parse(input))
  await verifyProofs(prepared)
  return prepared
}

/** Host-only compilation and local proof checks. Environment comes from verified
 * host assets/rates, never model arguments. No store, approval, model or network. */
export async function compileResearchExecution(input: ResearchContract, environment: ResearchExecutionEnvironment,
  binding: ResearchExecutionBinding, bounds?: { finish_before: number }): Promise<PreparedResearchExecution> {
  const contract = researchContract.parse(input)
  const window = bounds ? z.object({ finish_before: z.number().int().positive() }).strict().parse(bounds) : null
  const points = estimate(policy("research_pilot_v1"), { cpu: environment.cpu.points_per_hour, gpu: environment.gpu.points_per_hour })
  const compiled = compileScreening({ ...environment, directory: contract.directory, session: contract.session,
    intent: { target: contract.target.gene, site: contract.target.canonical_site,
      target_parameters: { target: contract.target.gene, receptor: contract.target.pdb, chain: contract.target.chain,
        residue: "CYS", residue_number: 797, insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" },
      library: "CovInDB/Virtual_Screening_Library.csv", screening_mode: "research_pilot_v1", max_points: points } })
  // Pass the original loop deadline on every round; compilation must never
  // reset that cumulative clock. A standalone draft still obeys its own cap.
  const deadline = Math.min(compiled.finish_before, compiled.created + contract.limits.max_elapsed_ms,
    window?.finish_before ?? Infinity)
  if (deadline <= compiled.created + 1) throw Error("Research execution deadline has expired")
  const plan = planSchema.parse({ ...compiled, approve_before: Math.min(compiled.approve_before, deadline - 1), finish_before: deadline })
  const prepared = validate(preparedResearchExecution.parse({ schema: "drug-design-research-execution-v1", contract,
    contract_digest: researchDigest(contract), binding, plan, plan_digest: digest(plan), resources: researchExecutionResources(plan) }))
  await verifyProofs(prepared)
  return prepared
}

const reference = z.object({ loop: z.string().min(1), key: z.string().min(1), contract_digest: researchHash }).strict()
export type ResearchExecutionReference = z.infer<typeof reference>
type BindingRow = { loop: string; key: string; contract_digest: string; task_id: string; plan_digest: string; prepared: string; digest: string }

function table(store: Store) {
  store.db.exec(`CREATE TABLE IF NOT EXISTS research_task_bindings(loop TEXT NOT NULL, key TEXT NOT NULL,
    contract_digest TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, plan_digest TEXT NOT NULL,
    prepared TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(loop,key));`)
}

function restore(row: BindingRow) {
  const prepared = validate(preparedResearchExecution.parse(JSON.parse(row.prepared)))
  if (researchDigest(prepared) !== row.digest || prepared.contract_digest !== row.contract_digest ||
    prepared.plan.id !== row.task_id || prepared.plan_digest !== row.plan_digest)
    throw Error("Native research binding integrity failure")
  return prepared
}

/** Persist an unapproved DRAFT and its identity in one native SQLite transaction.
 * Caller first persists the prepared plan and its cumulative research reservation.
 * A crash before research bindTask is recovered by this exact (loop,key), not a
 * newly compiled plan. This function never approves, starts a daemon or submits. */
export async function prepareResearchExecution(store: Store, input: PreparedResearchExecution, inputRef: ResearchExecutionReference) {
  const prepared = validate(preparedResearchExecution.parse(input))
  const ref = reference.parse(inputRef)
  if (ref.contract_digest !== prepared.contract_digest) throw Error("Research preparation belongs to another contract")
  await verifyProofs(prepared)
  table(store)
  return store.db.transaction(() => {
    const prior = store.db.query("SELECT * FROM research_task_bindings WHERE loop=? AND key=?").get(ref.loop, ref.key) as BindingRow | null
    if (prior) {
      if (prior.contract_digest !== ref.contract_digest || researchDigest(restore(prior)) !== researchDigest(prepared))
        throw Error("Research execution key already binds another frozen plan")
      const task = store.get(prior.task_id)
      if (task.digest !== prior.plan_digest) throw Error("Native task differs from its research binding")
      return { task, created: false, approved_by_adapter: false as const, submitted_by_adapter: false as const }
    }
    if (Date.now() > prepared.plan.approve_before) throw Error("Prepared native plan expired; no task was created")
    if (store.db.query("SELECT id FROM tasks WHERE id=?").get(prepared.plan.id))
      throw Error("Native task exists without this research binding; reconcile instead of adopting")
    const task = store.create(prepared.plan)
    store.db.query("INSERT INTO research_task_bindings VALUES(?,?,?,?,?,?,?)")
      .run(ref.loop, ref.key, ref.contract_digest, task.id, task.digest, JSON.stringify(prepared), researchDigest(prepared))
    store.event(task.id, "research_bound", { loop: ref.loop, key: ref.key, contract_digest: ref.contract_digest, plan_digest: task.digest })
    return { task, created: true, approved_by_adapter: false as const, submitted_by_adapter: false as const }
  }).immediate()
}

/** Read-only persisted native evidence. It does not recheck remote file bytes,
 * assert scientific objective success, or convert worker timings to billed usage. */
export function readResearchExecution(store: Store, input: ResearchExecutionReference) {
  const ref = reference.parse(input)
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='research_task_bindings'").get()) return null
  const row = store.db.query("SELECT * FROM research_task_bindings WHERE loop=? AND key=?").get(ref.loop, ref.key) as BindingRow | null
  if (!row) return null
  if (row.contract_digest !== ref.contract_digest) throw Error("Native task belongs to another research contract")
  const prepared = restore(row)
  const { task, stages } = verifiedExecution(store, row.task_id)
  if (task.digest !== prepared.plan_digest) throw Error("Native task differs from its research binding")
  return { schema: "drug-design-native-execution-snapshot-v1" as const, reference: ref, prepared, task, stages,
    reserved_resources: prepared.resources, usage: null, usage_status: "not_billing_evidence" as const,
    scientific_objective: "not_evaluated_by_adapter" as const, remote_artifacts_rehashed: false as const,
    events: store.events(task.id) }
}
