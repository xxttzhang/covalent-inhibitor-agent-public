import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, readFile, realpath } from "node:fs/promises"
import { researchDigest, researchHash, type ResearchContract } from "./research-loop-contract"
import { preparedReceptorChecks } from "./research-prepared-receptor"

/** Code-reviewed, actual first-pilot evidence. A model-authored/resealed proof
 * cannot register a different receptor, auditor, criterion or passing verdict. */
export const preparedReceptorV4 = Object.freeze({
  binding: { path: "reports/research-loop-20261013/receptor-preparation-v4/receptor-binding-v4.json", sha256: "6c531a9bada5939d583b0397af5d867184d1442c761bb268a531a7ecb784a328" },
  manifest: { path: "openscience-custom/backend/cli/integrations/discovery-platform/research-scientific-manifest-v4.json", sha256: "53d0877d21b4a183e8f0cd7b36a311cbc93d614ac35ead9b6e94b7c25a122cb0" },
  evaluator: { version: "egfr-library-scientific-evaluator-v4.0", path: "openscience-custom/backend/cli/integrations/discovery-platform/research-scientific-evaluator-v4.py", sha256: "e5a1abcb62a5d1c5d90e15c027d67d0fcb87297f7c75bc4822f2f8fed0327b85" },
})
const ref = z.object({ path: z.string(), sha256: researchHash }).strict()
const roles = ["source_receptor", "source_mapping_audit", "preparation_policy", "preparation_receipt", "prepared_receptor",
  "preparation_script", "atom_mapping", "independent_audit_policy", "independent_audit", "independent_auditor",
  "candidate_evaluator", "frozen_v1_evaluator", "frozen_v1_manifest"] as const
const binding = z.object({ schema: z.literal("drug-design-prepared-receptor-binding-v4"), method_revision: z.literal("4.0"),
  references: z.record(z.enum(roles), ref), scientific_status: z.enum(["pass", "fail"]), modeled_coordinates_are_experimental: z.literal(false) }).strict()
const check = z.object({ id: z.string(), status: z.enum(["pass", "fail", "unknown"]), measurements: z.record(z.string(), z.unknown()) }).passthrough()
const files = z.record(z.string(), researchHash)
const auditSchema = z.object({ schema: z.literal("drug-design-prepared-receptor-audit-v4"), method_revision: z.literal("4.0"),
  status: z.enum(["pass", "fail"]), source_sha256: researchHash, prepared_sha256: researchHash, mapping_sha256: researchHash,
  policy_sha256: researchHash, audit_policy_sha256: researchHash, evaluator_sha256: researchHash,
  preparation_receipt_sha256: researchHash, topology_sha256: researchHash, preparation_artifacts: files,
  initial_inputs: z.record(z.string(), ref), initial_artifacts: files, parent_v3_artifacts: files,
  audit_input_binding_complete: z.literal(true), frozen_v1_geometry: z.record(z.string(), z.number()),
  thresholds_changed_from_v1: z.literal(false), historical_scores_used_to_select_conformer: z.literal(false),
  required_checks: z.array(z.string()), checks: z.array(check), limitations: z.array(z.string()),
  runtime: z.object({ preparation_plus_audit_cpu_core_hours: z.number().finite().nonnegative(),
    previous_preparation_and_audits_cpu_core_hours: z.number().finite().nonnegative(),
    cumulative_cpu_core_hours: z.number().finite().positive(), process_cpu_seconds: z.number().finite().nonnegative(),
    new_gpu_compute: z.literal(false) }).passthrough(),
}).passthrough()

async function load(root: string, value: z.infer<typeof ref>) {
  const filename = path.resolve(root, value.path)
  if (!filename.startsWith(root + path.sep) || await realpath(filename) !== filename) throw Error("V4 receptor evidence escapes its immutable path")
  const before = await lstat(filename)
  if (!before.isFile() || before.size > 16 * 1024 * 1024) throw Error("V4 receptor evidence is not a bounded regular file")
  const bytes = await readFile(filename), after = await lstat(filename)
  if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs || createHash("sha256").update(bytes).digest("hex") !== value.sha256)
    throw Error("V4 receptor evidence bytes changed: " + path.basename(filename))
  return bytes
}
const same = (a: unknown, b: unknown) => researchDigest(a) === researchDigest(b)

export async function readPreparedReceptorV4(input: {
  root: string; value: unknown; contract: ResearchContract; geometry: Record<string, number>;
  evaluator: { version: string; path: string; sha256: string }; receptor_path: string; required_checks: string[]; core_sha256: string
}) {
  const value = binding.parse(input.value)
  const registered = binding.parse(JSON.parse((await load(input.root, preparedReceptorV4.binding)).toString()))
  if (!same(value, registered) || input.contract.target.mapping_sha256 !== preparedReceptorV4.binding.sha256)
    throw Error("V4 proof is not the registered actual audited receptor")
  const manifest = z.object({ geometry: z.record(z.string(), z.number()), target: z.object({ receptor_path: z.string(), receptor_sha256: researchHash }),
    prepared_receptor_semantics: z.object({ candidate_chemistry_and_geometry_core_sha256: researchHash }) }).passthrough()
    .parse(JSON.parse((await load(input.root, preparedReceptorV4.manifest)).toString()))
  if (input.contract.quality.manifest_sha256 !== preparedReceptorV4.manifest.sha256 || !same(input.geometry, manifest.geometry)
    || !same({ version: input.evaluator.version, path: input.evaluator.path, sha256: input.evaluator.sha256 }, preparedReceptorV4.evaluator)
    || input.receptor_path !== manifest.target.receptor_path
    || input.contract.target.receptor_sha256 !== manifest.target.receptor_sha256
    || input.core_sha256 !== manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256)
    throw Error("V4 science differs from the registered manifest/evaluator/target")
  const refs = value.references
  const bytes = Object.fromEntries(await Promise.all(roles.map(async role => [role, await load(input.root, refs[role])] as const))) as Record<typeof roles[number], Buffer>
  const audit = auditSchema.parse(JSON.parse(bytes.independent_audit.toString()))
  const receipt = z.object({ artifacts: files, initial_inputs: z.record(z.string(), ref), initial_artifacts: files, parent_v3_artifacts: files,
    source_unchanged: z.literal(true), status: z.literal("prepared_pending_independent_audit"),
    process_cpu_seconds: z.number().finite().nonnegative(), cpu_core_hours: z.number().finite().nonnegative(),
    stages: z.array(z.object({ stage: z.string(), artificial_chirality_restraint: z.boolean().optional(), soft_nonbonded: z.boolean().optional(), tolerance_reached: z.boolean().optional() }).passthrough()) }).passthrough()
    .parse(JSON.parse(bytes.preparation_receipt.toString()))
  const policy = z.object({ source: ref.passthrough(), v1_manifest: ref, preparation_script_sha256: researchHash,
    initial: z.record(z.string(), ref), geometry: z.record(z.string(), z.number()), compute: z.object({ maximum_cpu_core_hours: z.number().positive() }) }).passthrough()
    .parse(JSON.parse(bytes.preparation_policy.toString()))
  const review = z.object({ preparation_policy: ref, auditor: ref, initial_audit: ref, geometry: z.record(z.string(), z.number()), required_checks: z.array(z.string()) }).passthrough()
    .parse(JSON.parse(bytes.independent_audit_policy.toString()))
  const original = z.object({ structure_sha256: researchHash, evaluator_sha256: researchHash, entry_id: z.string(), uniprot: z.string(),
    uniprot_residue: z.number(), auth_asym_id: z.string(), author_residue_id: z.string(), reactive_atom: z.string(), taxon: z.number(),
    mapping_status: z.literal("pass"), checks: z.array(check) }).passthrough().parse(JSON.parse(bytes.source_mapping_audit.toString()))
  const target = input.contract.target
  if (original.entry_id !== target.pdb || original.uniprot !== target.uniprot || original.uniprot_residue !== 797
    || original.auth_asym_id !== target.chain || original.author_residue_id !== String(target.author_residue)
    || original.reactive_atom !== target.reactive_atom || original.taxon !== target.taxon
    || original.structure_sha256 !== refs.source_receptor.sha256 || original.evaluator_sha256 !== refs.frozen_v1_evaluator.sha256)
    throw Error("V4 original reactive-site identity differs from the research contract")
  for (const id of ["receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom"])
    if (original.checks.filter(item => item.id === id && item.status === "pass").length !== 1) throw Error("Original mapping identity check failed")
  for (const [field, digest] of Object.entries({ source_sha256: refs.source_receptor.sha256, prepared_sha256: refs.prepared_receptor.sha256,
    mapping_sha256: refs.atom_mapping.sha256, policy_sha256: refs.preparation_policy.sha256, audit_policy_sha256: refs.independent_audit_policy.sha256,
    evaluator_sha256: refs.independent_auditor.sha256, preparation_receipt_sha256: refs.preparation_receipt.sha256, topology_sha256: receipt.artifacts["topology.json"] }))
    if (audit[field] !== digest) throw Error("V4 audit lacks matching actual-input binding")
  if (!same(policy.source.path, refs.source_receptor.path) || policy.source.sha256 !== refs.source_receptor.sha256
    || !same(policy.v1_manifest, refs.frozen_v1_manifest) || policy.preparation_script_sha256 !== refs.preparation_script.sha256
    || !same(review.preparation_policy, refs.preparation_policy) || !same(review.auditor, refs.independent_auditor)
    || !same(policy.initial, receipt.initial_inputs) || !same(audit.initial_inputs, receipt.initial_inputs)
    || !same(audit.preparation_artifacts, receipt.artifacts) || !same(audit.initial_artifacts, receipt.initial_artifacts)
    || !same(audit.parent_v3_artifacts, receipt.parent_v3_artifacts)) throw Error("V4 preparation/audit lineage mismatch")
  for (const geometry of [policy.geometry, review.geometry, audit.frozen_v1_geometry])
    if (!same(geometry, manifest.geometry)) throw Error("V4 numerical criteria changed")
  for (const list of [input.required_checks, review.required_checks, audit.required_checks, audit.checks.map(item => item.id)])
    if (list.length !== preparedReceptorChecks.length || new Set(list).size !== list.length || preparedReceptorChecks.some(id => !list.includes(id)))
      throw Error("V4 required checks changed")
  const rehash = async (directory: string, artifacts: z.infer<typeof files>) => {
    for (const [name, sha256] of Object.entries(artifacts)) {
      if (path.basename(name) !== name || [".", ".."].includes(name)) throw Error("Invalid lineage artifact name")
      await load(input.root, { path: path.join(directory, name), sha256 })
    }
  }
  await rehash(path.dirname(refs.preparation_receipt.path), receipt.artifacts)
  for (const item of Object.values(receipt.initial_inputs)) await load(input.root, item)
  for (const [role, artifacts] of [["receipt", receipt.initial_artifacts], ["parent_v3_receipt", receipt.parent_v3_artifacts]] as const) {
    const parent = receipt.initial_inputs[role]!
    const content = z.object({ artifacts: files }).passthrough().parse(JSON.parse((await load(input.root, parent)).toString()))
    if (!same(content.artifacts, artifacts)) throw Error("V4 parent artifact receipt changed")
    await rehash(path.dirname(parent.path), artifacts)
  }
  await load(input.root, review.initial_audit)
  const last = receipt.stages.at(-2)
  if (Math.abs(receipt.cpu_core_hours - receipt.process_cpu_seconds / 3600) > 1e-12
    || Math.abs(audit.runtime.preparation_plus_audit_cpu_core_hours - (receipt.process_cpu_seconds + audit.runtime.process_cpu_seconds) / 3600) > 1e-12
    || Math.abs(audit.runtime.cumulative_cpu_core_hours - audit.runtime.previous_preparation_and_audits_cpu_core_hours
      - audit.runtime.preparation_plus_audit_cpu_core_hours) > 1e-12)
    throw Error("V4 cumulative CPU measurements contradict the bound preparation/audit receipts")
  if (last?.stage !== "amber_unrestrained" || last.artificial_chirality_restraint !== false || last.soft_nonbonded !== false || last.tolerance_reached !== true
    || audit.runtime.preparation_plus_audit_cpu_core_hours > policy.compute.maximum_cpu_core_hours)
    throw Error("V4 final preparation or resource scope is incomplete")
  const scientific_passed = audit.checks.every(item => item.status === "pass")
  if (audit.status !== (scientific_passed ? "pass" : "fail") || value.scientific_status !== audit.status) throw Error("V4 scientific summary contradicts detailed checks")
  return { source_sha256: refs.source_receptor.sha256, evaluator_sha256: refs.candidate_evaluator.sha256,
    prepared_sha256: refs.prepared_receptor.sha256, audit_sha256: refs.independent_audit.sha256, scientific_passed,
    admissible: scientific_passed, audit_input_binding_complete: true, limitations: audit.limitations, audit, references: refs }
}
