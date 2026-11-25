import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, readFile, realpath } from "node:fs/promises"
import { researchDigest, researchHash, type ResearchContract } from "./research-loop-contract"
import { readPreparedReceptorV4 } from "./research-prepared-receptor-v4"

export const preparedReceptorChecks = ["unique_atom_identity", "finite_coordinates", "atom_provenance_and_original_coordinates",
  "full_deposited_sequence_and_heavy_atom_completeness", "polymer_connectivity", "reactive_site_identity_and_prereaction_hydrogen",
  "standard_amino_acid_stereochemistry", "intramolecular_distance_bonds", "intramolecular_distance_angles",
  "intramolecular_distance_internal", "aromatic_planarity"]
const reference = z.object({ path: z.string().min(1), sha256: researchHash }).strict()
const references = ["source_receptor", "source_mapping_audit", "preparation_policy", "preparation_receipt",
  "prepared_receptor", "preparation_script", "atom_mapping", "independent_audit_policy", "independent_audit",
  "independent_auditor", "candidate_evaluator", "frozen_v1_evaluator", "frozen_v1_manifest"] as const
const binding = z.object({ schema: z.literal("drug-design-prepared-receptor-binding-v2"), method_revision: z.literal("2.1"),
  references: z.record(z.enum(references), reference), scientific_status: z.enum(["pass", "fail"]),
  modeled_coordinates_are_experimental: z.literal(false) }).passthrough()
const check = z.object({ id: z.string(), status: z.enum(["pass", "fail", "unknown"]),
  measurements: z.record(z.string(), z.unknown()) }).passthrough()
const auditSchema = z.object({ schema: z.literal("drug-design-prepared-receptor-audit-v2"), method_revision: z.literal("2.1"),
  status: z.enum(["pass", "fail"]), policy_sha256: researchHash, audit_policy_sha256: researchHash,
  source_sha256: researchHash, prepared_sha256: researchHash, mapping_sha256: researchHash, evaluator_sha256: researchHash,
  frozen_v1_geometry: z.record(z.string(), z.number()), thresholds_changed_from_v1: z.literal(false),
  historical_scores_used_to_select_conformer: z.literal(false), required_checks: z.array(z.string()), checks: z.array(check),
  runtime: z.object({ rdkit: z.string(), numpy: z.string(), elapsed_wall_seconds: z.number().nonnegative(),
    process_cpu_seconds: z.number().nonnegative(), preparation_plus_audit_cpu_core_hours: z.number().nonnegative(),
    new_gpu_compute: z.literal(false) }).passthrough(),
}).passthrough()
const mappingSchema = z.object({ schema: z.literal("drug-design-receptor-audit-v1"), structure_sha256: researchHash,
  evaluator_sha256: researchHash, entry_id: z.string(), uniprot: z.string(), uniprot_residue: z.number(),
  auth_asym_id: z.string(), author_residue_id: z.string(), reactive_atom: z.string(), taxon: z.number(),
  mapping_status: z.literal("pass"), checks: z.array(check) }).passthrough()
const preparationSchema = z.object({ schema: z.literal("drug-design-receptor-preparation-policy-v2"),
  source: reference.passthrough(), preparation_script_sha256: researchHash,
  geometry: z.record(z.string(), z.number()), v1_manifest: reference,
}).passthrough()
const auditPolicySchema = z.object({ schema: z.literal("drug-design-prepared-receptor-audit-policy-v2"), method_revision: z.literal("2.1"),
  preparation_policy: reference, auditor: reference, initial_audit: reference, required_checks: z.array(z.string()),
  geometry: z.record(z.string(), z.number()), thresholds_changed_from_v1: z.literal(false), coordinate_changes: z.literal(false),
}).passthrough()
const receiptSchema = z.object({ schema: z.literal("drug-design-receptor-preparation-receipt-v2"),
  status: z.literal("prepared_pending_independent_audit"), source_sha256: researchHash, policy_sha256: researchHash,
  script_sha256: researchHash, source_unchanged: z.literal(true), modeled_coordinates_are_experimental: z.literal(false),
  artifacts: z.record(z.string(), researchHash), process_cpu_seconds: z.number().nonnegative(),
}).passthrough()
const atomsSchema = z.object({ source_sha256: researchHash, prepared_sha256: researchHash, policy_sha256: researchHash,
  atoms: z.array(z.unknown()).min(1) }).passthrough()

/** Every referenced proof is a real, contained local file. This is read-only
 * semantic admission, not an execution of the supplied evaluator program. */
async function load(root: string, value: z.infer<typeof reference>) {
  const filename = path.resolve(root, value.path)
  if (!filename.startsWith(root + path.sep) || await realpath(filename) !== filename)
    throw Error("Prepared receptor evidence must be contained without symlink substitution")
  const stat = await lstat(filename)
  if (!stat.isFile() || stat.size > 16 * 1024 * 1024) throw Error("Prepared receptor evidence is not a bounded regular file")
  const bytes = await readFile(filename)
  if (createHash("sha256").update(bytes).digest("hex") !== value.sha256)
    throw Error("Prepared receptor evidence bytes changed: " + path.basename(filename))
  return bytes
}

export async function readPreparedReceptorEvidence(input: {
  root: string; value: unknown; contract: ResearchContract; geometry: Record<string, number>;
  evaluator: { version: string; path: string; sha256: string }; receptor_path: string; required_checks: string[]; core_sha256: string
}) {
  if (typeof input.value === "object" && input.value !== null && "schema" in input.value
    && input.value.schema === "drug-design-prepared-receptor-binding-v4") return readPreparedReceptorV4(input)
  const value = binding.parse(input.value)
  const refs = value.references
  const entries = await Promise.all(references.map(async key => [key, await load(input.root, refs[key])] as const))
  const bytes = Object.fromEntries(entries) as Record<typeof references[number], Buffer>
  const audit = auditSchema.parse(JSON.parse(bytes.independent_audit.toString("utf8")))
  const original = mappingSchema.parse(JSON.parse(bytes.source_mapping_audit.toString("utf8")))
  const policy = preparationSchema.parse(JSON.parse(bytes.preparation_policy.toString("utf8")))
  const reviewed = auditPolicySchema.parse(JSON.parse(bytes.independent_audit_policy.toString("utf8")))
  const receipt = receiptSchema.parse(JSON.parse(bytes.preparation_receipt.toString("utf8")))
  const atoms = atomsSchema.parse(JSON.parse(bytes.atom_mapping.toString("utf8")))
  // The frozen 2.1 auditor never emitted this field. An extra manually supplied
  // hash cannot retroactively prove which receipt/topology that auditor read.
  // A future bound-input auditor requires its own registered revision/policy.
  if (Object.hasOwn(audit, "preparation_receipt_sha256")) {
    if (audit.preparation_receipt_sha256 !== refs.preparation_receipt.sha256)
      throw Error("Prepared receptor audit receipt hash differs from its bound preparation receipt")
    throw Error("Prepared receptor audit 2.1 does not support an added preparation receipt hash; a new registered auditor is required")
  }
  const target = input.contract.target
  for (const [actual, expected] of [[policy.source, refs.source_receptor], [policy.v1_manifest, refs.frozen_v1_manifest],
    [reviewed.preparation_policy, refs.preparation_policy], [reviewed.auditor, refs.independent_auditor]])
    if (path.resolve(input.root, actual!.path) !== path.resolve(input.root, expected!.path))
      throw Error("Prepared receptor policy reads a different file from its bound evidence role")
  if (input.evaluator.version !== "egfr-library-scientific-evaluator-v2.1" ||
    input.evaluator.sha256 !== refs.candidate_evaluator.sha256 ||
    path.resolve(input.root, input.evaluator.path) !== path.resolve(input.root, refs.candidate_evaluator.path) ||
    path.resolve(input.root, input.receptor_path) !== path.resolve(input.root, refs.prepared_receptor.path) ||
    refs.prepared_receptor.sha256 !== target.receptor_sha256 || input.core_sha256 !== refs.frozen_v1_evaluator.sha256)
    throw Error("Prepared receptor or candidate evaluator differs from the frozen science contract")
  if (original.structure_sha256 !== refs.source_receptor.sha256 || original.evaluator_sha256 !== refs.frozen_v1_evaluator.sha256 ||
    original.entry_id !== target.pdb || original.uniprot !== target.uniprot || original.uniprot_residue !== 797 ||
    original.auth_asym_id !== target.chain || original.author_residue_id !== String(target.author_residue) ||
    original.reactive_atom !== target.reactive_atom || original.taxon !== target.taxon)
    throw Error("Prepared receptor source mapping differs from the contract's species and reactive site")
  for (const id of ["receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom"])
    if (original.checks.filter(item => item.id === id).length !== 1 || original.checks.find(item => item.id === id)?.status !== "pass")
      throw Error("Original receptor mapping lacks a required passing identity check")
  if (original.checks.find(item => item.id === "receptor_bytes")?.measurements.sha256 !== refs.source_receptor.sha256)
    throw Error("Original receptor mapping does not bind its exact source bytes")
  if (audit.source_sha256 !== refs.source_receptor.sha256 || audit.prepared_sha256 !== refs.prepared_receptor.sha256 ||
    audit.mapping_sha256 !== refs.atom_mapping.sha256 || audit.policy_sha256 !== refs.preparation_policy.sha256 ||
    audit.audit_policy_sha256 !== refs.independent_audit_policy.sha256 || audit.evaluator_sha256 !== refs.independent_auditor.sha256 ||
    policy.source.sha256 !== refs.source_receptor.sha256 || policy.preparation_script_sha256 !== refs.preparation_script.sha256 ||
    policy.v1_manifest.sha256 !== refs.frozen_v1_manifest.sha256 || reviewed.preparation_policy.sha256 !== refs.preparation_policy.sha256 ||
    reviewed.auditor.sha256 !== refs.independent_auditor.sha256 || receipt.source_sha256 !== refs.source_receptor.sha256 ||
    receipt.policy_sha256 !== refs.preparation_policy.sha256 || receipt.script_sha256 !== refs.preparation_script.sha256 ||
    atoms.source_sha256 !== refs.source_receptor.sha256 || atoms.prepared_sha256 !== refs.prepared_receptor.sha256 ||
    atoms.policy_sha256 !== refs.preparation_policy.sha256)
    throw Error("Prepared receptor provenance hashes do not form the same preparation and audit chain")
  const frozen = z.object({ geometry: z.record(z.string(), z.number()), evaluator: z.object({ sha256: researchHash }) }).passthrough()
    .parse(JSON.parse(bytes.frozen_v1_manifest.toString("utf8")))
  if (frozen.evaluator.sha256 !== refs.frozen_v1_evaluator.sha256 ||
    [policy.geometry, reviewed.geometry, audit.frozen_v1_geometry, frozen.geometry].some(item => researchDigest(item) !== researchDigest(input.geometry)))
    throw Error("Prepared receptor audit changed the registered numerical geometry thresholds")
  for (const values of [input.required_checks, reviewed.required_checks, audit.required_checks, audit.checks.map(item => item.id)])
    if (values.length !== preparedReceptorChecks.length || new Set(values).size !== values.length ||
      preparedReceptorChecks.some(id => !values.includes(id))) throw Error("Prepared receptor audit has missing or unsupported mandatory checks")
  // All preparation artifacts are rehashed, including original atom decisions,
  // topology and intermediate inputs; a summary cannot replace that lineage.
  const receiptRoot = path.dirname(refs.preparation_receipt.path)
  for (const name of ["atom-mapping.json", "topology.json", "original-atom-decisions.json", "rebuild-input.pdb",
    "rebuild-manifest.json", "openmm-output.pdb", "1M17-prepared-model-v2.pdb"])
    if (!receipt.artifacts[name]) throw Error("Preparation receipt omits a required provenance artifact")
  for (const [name, sha256] of Object.entries(receipt.artifacts)) {
    if (path.basename(name) !== name || name === "." || name === "..") throw Error("Invalid preparation receipt artifact path")
    await load(input.root, { path: path.join(receiptRoot, name), sha256 })
  }
  if (receipt.artifacts[path.basename(refs.prepared_receptor.path)] !== refs.prepared_receptor.sha256 ||
    receipt.artifacts[path.basename(refs.atom_mapping.path)] !== refs.atom_mapping.sha256)
    throw Error("Preparation receipt does not bind the prepared receptor and atom provenance")
  await load(input.root, reviewed.initial_audit)
  const scientific_passed = audit.checks.every(item => item.status === "pass")
  if (value.scientific_status !== audit.status || audit.status !== (scientific_passed ? "pass" : "fail"))
    throw Error("Prepared receptor audit summary contradicts its individual checks")
  return { source_sha256: refs.source_receptor.sha256, evaluator_sha256: refs.candidate_evaluator.sha256,
    prepared_sha256: refs.prepared_receptor.sha256, audit_sha256: refs.independent_audit.sha256,
    scientific_passed, admissible: false, audit_input_binding_complete: false,
    limitations: ["Auditor 2.1 did not bind the preparation receipt and its topology/artifact hashes. Current file integrity does not establish that these were the audited inputs. Read-only historical evidence; native execution requires a new registered auditor and policy with complete audited-input binding."],
    audit, references: refs }
}

/** Native computation additionally requires scientific readiness. A verified
 * failed audit may inform a model, but can never authorize computation. */
export async function verifyPreparedReceptorEvidence(input: Parameters<typeof readPreparedReceptorEvidence>[0]) {
  const value = await readPreparedReceptorEvidence(input)
  if (!value.scientific_passed) throw Error("Prepared receptor audit must pass every required check before native preparation")
  if (!value.audit_input_binding_complete)
    throw Error("Prepared receptor audit 2.1 has unsupported audited-input binding; a new registered auditor must bind the preparation receipt before native preparation")
  return value
}
