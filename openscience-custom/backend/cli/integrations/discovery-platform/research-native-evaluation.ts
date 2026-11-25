import path from "node:path"
import { createHash } from "node:crypto"
import { constants } from "node:fs"
import { mkdir, open, readFile, realpath, writeFile } from "node:fs/promises"
import z from "zod"
import { emptyResources, researchDigest, researchHash } from "./research-loop-contract"
import { readResearchNativeAction } from "./research-native-controller"
import { observeResearchNativeAction } from "./research-native-observation"
import { readResearchNativeReceipt } from "./research-native"
import { createResearchObservation, type ResearchObservation } from "./research-observations"
import { verifyPreparedReceptorEvidence } from "./research-prepared-receptor"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"

type Identity = Parameters<typeof readResearchNativeAction>[0] & { abort?: AbortSignal }
const sha = (value: Uint8Array | string) => createHash("sha256").update(value).digest("hex")
const base = "openscience-custom/backend/cli/integrations/discovery-platform/"
/** Explicit code-reviewed registry. New scientific versions require a new
 * adapter/output schema and input-binding tests, never a model-supplied script. */
export const researchScientificEvaluatorRegistry = Object.freeze({
  "egfr-library-scientific-evaluator-v1": Object.freeze({
    evaluator: base + "research-scientific-evaluator.py", evaluator_sha256: "ba6829a12d96df43e6c3b53079c003dcb65bf88e13c94f2667db03f1687ae0b8",
    manifest: base + "research-scientific-manifest.json", manifest_sha256: "d2d90d4f6f07963d7736e4bad80ea89feb72b1a37c7ad3cb84c89e8a714a2611",
    interpreter: ".tooling/design-route-validation/bin/python", interpreter_sha256: "ac716b2c210ccdab309b214b753a1f7a3e5a783bb945e0061632421c987bae28",
    python_version: "3.13.5", cpu_seconds: 60, wall_ms: 90000, threads: 1,
  }),
  "egfr-library-scientific-evaluator-v4.0": Object.freeze({
    evaluator: preparedReceptorV4.evaluator.path, evaluator_sha256: preparedReceptorV4.evaluator.sha256,
    manifest: preparedReceptorV4.manifest.path, manifest_sha256: preparedReceptorV4.manifest.sha256,
    interpreter: ".tooling/design-route-validation/bin/python", interpreter_sha256: "ac716b2c210ccdab309b214b753a1f7a3e5a783bb945e0061632421c987bae28",
    python_version: "3.13.5", cpu_seconds: 60, wall_ms: 90000, threads: 1,
  }),
})
const version = "egfr-library-scientific-evaluator-v1" as const
const required = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility", "pose_geometry", "registered_objective_acceptance"]
const roles = { candidates_csv: "prepare/stage2_candidates.csv", candidates_sdf: "prepare/stage2_candidates.sdf",
  docking_csv: "carsidock/scores.csv", poses_sdf: "carsidock/carsidock.sdf", scoring_csv: "rtmscore/scores.csv" } as const
const status = z.enum(["pass", "fail", "unknown"])
const scientificCheck = z.object({ id: z.string().min(1), status, detail: z.string(), measurements: z.record(z.string(), z.unknown()) }).strict()
const resultSchema = z.object({ schema: z.literal("drug-design-scientific-evaluation-v1"), evaluator: z.literal(version),
  evaluator_sha256: researchHash, manifest_sha256: researchHash, origin: z.literal("local_artifact_evaluation"), new_remote_compute: z.literal(false),
  status, outcome: z.enum(["objective_met", "no_viable_candidate", "insufficient_evidence"]),
  receptor: z.object({ schema: z.literal("drug-design-receptor-audit-v1"), structure_sha256: researchHash,
    evaluator_sha256: researchHash, status, mapping_status: status, checks: z.array(scientificCheck) }).passthrough(),
  candidate_count: z.number().int().nonnegative().max(5), counts: z.object({ pass: z.number().int().nonnegative(), fail: z.number().int().nonnegative(), unknown: z.number().int().nonnegative() }).strict(),
  candidates: z.array(z.object({ compound_id: z.string(), input_sdf_index: z.number().int().nonnegative(), status, checks: z.array(scientificCheck) }).strict()).max(5),
  artifacts: z.record(z.string(), z.object({ path: z.string(), sha256: researchHash }).strict()),
  accepted_candidate_ids: z.array(z.string()), conclusion_scope: z.literal("computational-shortlist-only"), limitations: z.array(z.string()),
}).strict()
const resultV4Schema = resultSchema.extend({ schema: z.literal("drug-design-scientific-evaluation-v4"), evaluator: z.literal("egfr-library-scientific-evaluator-v4.0"),
  native_execution_provenance_verified: z.literal(false),
  receptor: z.object({ schema: z.literal("drug-design-prepared-receptor-proof-evaluation-v4"), structure_sha256: researchHash,
    source_structure_sha256: researchHash, binding_sha256: researchHash, audit_sha256: researchHash,
    status, mapping_status: status, checks: z.array(scientificCheck) }).passthrough(),
})
const manifestSchema = z.object({ schema: z.enum(["drug-design-scientific-manifest-v1", "drug-design-scientific-manifest-v4"]),
  version: z.enum(["egfr-c797-library-computational-shortlist-v1", "egfr-c797-library-computational-shortlist-v4.0"]),
  evaluator: z.object({ version: z.enum([version, "egfr-library-scientific-evaluator-v4.0"]), path: z.string(), sha256: researchHash }).passthrough(),
  target: z.object({ receptor_path: z.string(), receptor_sha256: researchHash }).passthrough(),
  recipe: z.object({ path: z.string(), sha256: researchHash }).passthrough(),
  scope: z.object({ minimum_accepted_candidates: z.number().int().min(1).max(5), dataset_manifest_path: z.string(), dataset_manifest_sha256: researchHash }).passthrough(),
  artifacts: z.record(z.string(), z.string()),
  geometry: z.record(z.string(), z.number()).optional(), required_receptor_checks: z.array(z.string()).optional(),
  prepared_receptor_semantics: z.object({ candidate_chemistry_and_geometry_core_sha256: researchHash }).passthrough().optional(),
}).passthrough()
const proofSchema = z.object({ schema: z.literal("drug-design-receptor-audit-v1"), structure_sha256: researchHash, evaluator_sha256: researchHash,
  entry_id: z.literal("1M17"), uniprot: z.literal("P00533"), uniprot_residue: z.literal(797), auth_asym_id: z.literal("A"),
  author_residue_id: z.literal("773"), reactive_atom: z.literal("SG"), taxon: z.literal(9606), mapping_status: z.literal("pass"),
  checks: z.array(scientificCheck) }).passthrough()
const fileRef = z.object({ path: z.string(), sha256: researchHash }).strict()
const resourceReceiptSchema = z.object({ schema: z.literal("drug-design-host-scientific-resource-receipt-v1"), attempt_digest: researchHash,
  source_receipt_digest: researchHash, pid: z.number().int().positive(), exit_code: z.number().int(), signal: z.string().nullable(),
  elapsed_ms: z.number().finite().nonnegative(), cpu_user_microseconds: z.number().int().nonnegative(), cpu_system_microseconds: z.number().int().nonnegative(),
  usage_origin: z.literal("host_Bun_Subprocess_resourceUsage_wait4"), output_limit_exceeded: z.boolean(),
}).strict()
const receiptSchema = z.object({ schema: z.literal("drug-design-host-scientific-process-receipt-v1"), attempt_digest: researchHash,
  source_receipt_digest: researchHash, evaluator_sha256: researchHash, manifest_sha256: researchHash,
  wrapper_sha256: researchHash, interpreter_sha256: researchHash, pid: z.number().int().positive(),
  exit_code: z.number().int(), signal: z.string().nullable(), elapsed_ms: z.number().finite().nonnegative(),
  cpu_user_microseconds: z.number().finite().nonnegative(), cpu_system_microseconds: z.number().finite().nonnegative(),
  usage_origin: z.literal("host_Bun_Subprocess_resourceUsage_wait4"), output: fileRef.nullable(),
  resource_receipt_ref: z.string(), resource_receipt_digest: researchHash,
  stdout: fileRef.nullable(), stderr: fileRef.nullable(), output_limit_exceeded: z.boolean(), error: z.string().nullable(),
}).strict()
const attemptSchema = z.object({ schema: z.literal("drug-design-host-scientific-attempt-v1"), key: z.string(),
  task_id: z.string(), plan_digest: researchHash, contract_digest: researchHash, decision_id: z.string(),
  source_observation_id: z.string(), source_receipt_ref: z.string(), source_receipt_digest: researchHash,
  manifest_sha256: researchHash, evaluator_sha256: researchHash, interpreter_sha256: researchHash, wrapper_sha256: researchHash,
  directory: z.string(), inputs: z.array(fileRef), artifacts: z.record(z.string(), fileRef),
  cpu_seconds: z.number().positive(), wall_ms: z.number().int().positive(), created: z.number().int(),
}).strict()
type Attempt = z.infer<typeof attemptSchema>
const wrapper = `import hashlib,json,os,resource,runpy,sys\nconfig=json.loads(sys.argv[1])\nif sys.version.split()[0]!=config['python_version']: raise RuntimeError('Registered Python version changed')\nresource.setrlimit(resource.RLIMIT_CPU,(config['cpu_seconds']-1,config['cpu_seconds']))\nresource.setrlimit(resource.RLIMIT_FSIZE,(8*1024*1024,8*1024*1024))\nfor item in config['inputs']:\n if hashlib.sha256(open(item['path'],'rb').read()).hexdigest()!=item['sha256']: raise RuntimeError('Frozen evaluator input changed')\nsys.argv=[config['script'],'--manifest',config['manifest'],'--pdb',config['receptor'],'--artifacts',config['artifacts'],'--output',config['output']]\nrunpy.run_path(config['script'],run_name='__main__')\n`

async function boundedFile(filename: string, expected?: string, maximum = 128 * 1024 * 1024) {
  if (await realpath(filename) !== filename) throw Error("Scientific file path was redirected")
  const handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const before = await handle.stat()
    if (!before.isFile() || before.size > maximum) throw Error("Scientific file is not a bounded regular file")
    const bytes = await handle.readFile(), after = await handle.stat(), digest = sha(bytes)
    if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs || (expected && digest !== expected))
      throw Error("Scientific evidence bytes changed")
    return { path: filename, sha256: digest, bytes }
  } finally { await handle.close() }
}
function contained(root: string, filename: string) {
  const result = path.resolve(root, filename)
  if (!result.startsWith(root + path.sep)) throw Error("Scientific input escapes research directory")
  return result
}
function receiptDigest(receipt: Awaited<ReturnType<typeof readResearchNativeReceipt>>) {
  const { observed_at: _, ...value } = receipt
  return researchDigest(value)
}
function combined(checks: { status: z.infer<typeof status> }[]) {
  return checks.some(check => check.status === "fail") ? "fail" : !checks.length || checks.some(check => check.status === "unknown") ? "unknown" : "pass"
}

async function context(input: Identity) {
  const action = readResearchNativeAction(input), root = await realpath(action.loop.contract.directory)
  const captured = await observeResearchNativeAction({ ...input, decision_id: action.decision_id })
  if (!captured.observation || captured.receipt.task_state !== "SUCCEEDED") return { ready: false as const, reason: "Completed successful native candidate Results are required", captured }
  const observation = captured.observation
  if (observation.origin !== "native_execution" || observation.run.task_id !== captured.receipt.task_id || observation.identity.status !== "pass"
    || observation.integrity_errors.length || observation.execution_checks.some(check => check.status !== "pass") || !observation.candidates.length
    || observation.counts.selected !== observation.candidates.length || observation.artifacts.some(file => file.status !== "verified"))
    throw Error("Native result identity, denominator or delivered artifact integrity is incomplete")
  const contract = action.loop.contract
  if (contract.mode !== "native-compute" || !contract.allowed_adaptations.includes("evaluate")) throw Error("Contract does not permit local scientific evaluation")
  if (researchDigest(contract.quality.required_checks) !== researchDigest(required)) throw Error("Unregistered scientific check set")
  const selected = Object.entries(researchScientificEvaluatorRegistry).find(([, item]) => item.manifest_sha256 === contract.quality.manifest_sha256)
  if (!selected) throw Error("Scientific evaluator/manifest version is not registered for execution; prepared v2.1 remains read-only")
  const [registeredVersion, registry] = selected
  if (path.resolve(action.prepared.binding.quality_manifest) !== contained(root, registry.manifest))
    throw Error("Scientific evaluator/manifest version is not registered for execution; prepared v2.1 remains read-only")
  const manifestFile = await boundedFile(contained(root, registry.manifest), registry.manifest_sha256, 1024 * 1024)
  const manifest = manifestSchema.parse(JSON.parse(manifestFile.bytes.toString()))
  if (manifest.evaluator.version !== registeredVersion || manifest.evaluator.sha256 !== registry.evaluator_sha256 || manifest.evaluator.path !== registry.evaluator || researchDigest(manifest.artifacts) !== researchDigest(roles)
    || manifest.target.receptor_sha256 !== contract.target.receptor_sha256 || manifest.recipe.sha256 !== contract.inputs.science_config_sha256
    || manifest.scope.dataset_manifest_sha256 !== contract.inputs.subset_sha256 || manifest.scope.minimum_accepted_candidates !== contract.quality.minimum_candidates
    || contained(root, manifest.scope.dataset_manifest_path) !== action.prepared.binding.subset_manifest)
    throw Error("Frozen evaluator inputs do not match the native scientific contract")
  const files = await Promise.all([
    boundedFile(contained(root, registry.evaluator), registry.evaluator_sha256, 1024 * 1024),
    boundedFile(contained(root, manifest.target.receptor_path), manifest.target.receptor_sha256),
    boundedFile(contained(root, manifest.recipe.path), manifest.recipe.sha256, 1024 * 1024),
    boundedFile(contained(root, action.prepared.binding.mapping), contract.target.mapping_sha256!, 8 * 1024 * 1024),
    boundedFile(contained(root, action.prepared.binding.subset_manifest), contract.inputs.subset_sha256!, 16 * 1024 * 1024),
  ])
  const preparedInput = manifest.schema === "drug-design-scientific-manifest-v4" ? { root,
    value: JSON.parse(files[3]!.bytes.toString()), contract, geometry: manifest.geometry!, evaluator: manifest.evaluator,
    receptor_path: manifest.target.receptor_path, required_checks: manifest.required_receptor_checks!,
    core_sha256: manifest.prepared_receptor_semantics!.candidate_chemistry_and_geometry_core_sha256 } : null
  const prepared = preparedInput ? await verifyPreparedReceptorEvidence(preparedInput) : null
  if (!prepared) {
    const proof = proofSchema.parse(JSON.parse(files[3]!.bytes.toString()))
    if (proof.structure_sha256 !== manifest.target.receptor_sha256 || proof.evaluator_sha256 !== registry.evaluator_sha256
      || ["receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom"].some(id => proof.checks.filter(check => check.id === id && check.status === "pass").length !== 1))
      throw Error("Receptor identity proof does not bind the frozen evaluator")
  }
  // Scientific coordinate failure is deliberately allowed at evaluation time;
  // the frozen evaluator must report unknown. This does not grant native admission.
  const interpreter = contained(root, registry.interpreter), resolvedInterpreter = await realpath(interpreter)
  if (sha(await readFile(resolvedInterpreter)) !== registry.interpreter_sha256) throw Error("Unregistered scientific interpreter bytes")
  const artifacts: Record<string, { path: string; sha256: string }> = {}
  for (const [role, name] of Object.entries(roles)) {
    const [phase, basename] = name.split("/")
    const matches = captured.receipt.files.filter(file => file.phase === phase && file.name === basename)
    if (matches.length !== 1 || matches[0]!.status !== "verified" || !matches[0]!.receipt) throw Error("Required pose/score artifact lacks native Results provenance")
    artifacts[role] = { path: matches[0]!.local_path, sha256: matches[0]!.expected_sha256 }
  }
  return { ready: true as const, root, action, captured, observation, manifest, interpreter, registry, prepared, preparedInput,
    inputs: [manifestFile, ...files].map(({ path, sha256 }) => ({ path, sha256 })), artifacts }
}

function validateOutput(value: unknown, attempt: Attempt, observation: ResearchObservation, receptorHash: string, minimum: number,
  prepared: Extract<Awaited<ReturnType<typeof context>>, { ready: true }>["prepared"]) {
  const result = prepared ? resultV4Schema.parse(value) : resultSchema.parse(value)
  if (result.receptor.schema === "drug-design-prepared-receptor-proof-evaluation-v4") {
    if (!prepared || result.receptor.binding_sha256 !== preparedReceptorV4.binding.sha256
      || result.receptor.audit_sha256 !== prepared.audit_sha256 || result.receptor.source_structure_sha256 !== prepared.source_sha256
      || result.receptor.status !== prepared.audit.status || researchDigest(result.receptor.checks) !== researchDigest(prepared.audit.checks))
      throw Error("Scientific prepared receptor output differs from the actual registered audit")
  } else if (result.receptor.evaluator_sha256 !== attempt.evaluator_sha256) throw Error("Scientific receptor evaluator identity changed")
  if (result.evaluator_sha256 !== attempt.evaluator_sha256 || result.manifest_sha256 !== attempt.manifest_sha256
    || result.receptor.structure_sha256 !== receptorHash
    || result.candidate_count !== observation.candidates.length || result.candidates.length !== observation.candidates.length
    || new Set(result.candidates.map(item => item.compound_id)).size !== result.candidates.length
    || result.candidates.some((item, index) => item.input_sdf_index !== index || item.compound_id !== observation.candidates[index]?.id))
    throw Error("Scientific output candidate/source identity or denominator differs from native Results")
  for (const [role, relative] of Object.entries(roles)) {
    const record = result.artifacts[role], original = attempt.artifacts[role]
    if (!record || record.path !== path.join(attempt.directory, "artifacts", relative) || record.sha256 !== original?.sha256)
      throw Error("Scientific evaluator did not consume the frozen native artifact snapshot")
  }
  const chemistry = ["molecular_identity", "registered_lead_like", "declared_warhead", "molecule_sanity"]
  const geometry = ["covalent_product_identity", "retained_stereochemistry", "covalent_anchor", "bond_lengths", "bond_angles", "internal_clashes", "aromatic_planarity", "protein_clashes"]
  for (const candidate of result.candidates) {
    if (new Set(candidate.checks.map(check => check.id)).size !== candidate.checks.length || combined(candidate.checks) !== candidate.status)
      throw Error("Scientific candidate status contradicts its checks")
    if (candidate.status === "pass" && [...chemistry, ...geometry, "finite_engine_scores"].some(id => candidate.checks.find(check => check.id === id)?.status !== "pass"))
      throw Error("Scientific pass omits a mandatory registered candidate check")
  }
  for (const state of ["pass", "fail", "unknown"] as const)
    if (result.counts[state] !== result.candidates.filter(item => item.status === state).length) throw Error("Scientific status counts do not match candidates")
  const accepted = result.candidates.filter(item => item.status === "pass").map(item => item.compound_id)
  if (researchDigest(accepted) !== researchDigest(result.accepted_candidate_ids)) throw Error("Accepted candidate list is inconsistent")
  const outcome = result.receptor.status !== "pass" || result.counts.unknown ? "insufficient_evidence" : accepted.length >= minimum ? "objective_met" : "no_viable_candidate"
  if (result.outcome !== outcome || result.status !== (outcome === "insufficient_evidence" ? "unknown" : outcome === "objective_met" ? "pass" : "fail"))
    throw Error("Scientific output claims an unsupported objective")
  return { result, chemistry, geometry }
}

async function finish(input: Identity, current: Extract<Awaited<ReturnType<typeof context>>, { ready: true }>, attempt: Attempt) {
  const registry = current.registry
  if (current.preparedInput) await verifyPreparedReceptorEvidence(current.preparedInput)
  const row = input.research.evidence(input.id, "host").find(row => row.key === `evaluation-process:${attempt.key}`)
  if (!row) return { status: "needs_reconciliation" as const, reason: input.research.evidence(input.id, "host").some(row => row.key === `evaluation-resource:${attempt.key}`)
    ? "Host CPU receipt is retained but result persistence is incomplete; reconcile explicitly and never automatically rerun"
    : "Execution claimed but no complete host resource receipt exists; never automatically rerun", evaluation_ref: null, observation: null,
    receipt_ref: attempt.source_receipt_ref, reservation_key: attempt.key, loop: input.research.get(input.id) }
  const receipt = receiptSchema.parse(row.payload)
  const usageRow = input.research.evidence(input.id, "host").find(row => row.key === receipt.resource_receipt_ref)
  const usage = resourceReceiptSchema.parse(usageRow?.payload)
  if (receipt.resource_receipt_ref !== `evaluation-resource:${attempt.key}` || receipt.resource_receipt_digest !== researchDigest(usage)
    || usage.attempt_digest !== researchDigest(attempt) || usage.source_receipt_digest !== attempt.source_receipt_digest
    || ["pid", "exit_code", "signal", "elapsed_ms", "cpu_user_microseconds", "cpu_system_microseconds", "usage_origin", "output_limit_exceeded"]
      .some(key => usage[key as keyof typeof usage] !== receipt[key as keyof typeof receipt])) throw Error("Scientific resource receipt is inconsistent")
  if (receipt.attempt_digest !== researchDigest(attempt) || receipt.source_receipt_digest !== receiptDigest(current.captured.receipt)
    || receipt.evaluator_sha256 !== registry.evaluator_sha256 || receipt.manifest_sha256 !== registry.manifest_sha256
    || receipt.interpreter_sha256 !== registry.interpreter_sha256 || receipt.wrapper_sha256 !== sha(wrapper)) throw Error("Scientific process receipt identity changed")
  await Promise.all([...[receipt.stdout, receipt.stderr].filter((item): item is z.infer<typeof fileRef> => item !== null).map(item => boundedFile(item.path, item.sha256)),
    ...attempt.inputs.map(item => boundedFile(item.path, item.sha256)), ...Object.values(attempt.artifacts).map(item => boundedFile(item.path, item.sha256)),
    ...Object.entries(roles).map(([role, relative]) => boundedFile(path.join(attempt.directory, "artifacts", relative), attempt.artifacts[role]!.sha256))])
  const output = receipt.output ? await boundedFile(receipt.output.path, receipt.output.sha256, 8 * 1024 * 1024) : null
  const native = await readResearchNativeReceipt({ research: input.research, native: input.native, reference: current.captured.reference })
  if (receiptDigest(native) !== attempt.source_receipt_digest) throw Error("Native source changed after scientific evaluation")
  const measured = { ...emptyResources(), cpu_core_hours: (receipt.cpu_user_microseconds + receipt.cpu_system_microseconds) / 3_600_000_000 }
  const ref = `scientific-evaluation:${attempt.key}`
  const stderr = receipt.stderr ? await boundedFile(receipt.stderr.path, receipt.stderr.sha256) : null
  const stderrRef = `scientific-stderr:${attempt.key}`
  const artifacts = [...current.observation.artifacts,
    ...(stderr ? [{ ref: stderrRef, path: stderr.path, sha256: stderr.sha256, expected_sha256: stderr.sha256, size_bytes: stderr.bytes.length, status: "verified" as const }] : []),
    ...(output ? [{ ref, path: output.path, sha256: output.sha256, expected_sha256: output.sha256, size_bytes: output.bytes.length, status: "verified" as const }] : [])]
  let parsed: ReturnType<typeof validateOutput> | null = null, error = receipt.error, invalidOutput: string | null = receipt.error
  try {
    if (!receipt.error && receipt.exit_code === 0 && !receipt.signal && !receipt.output_limit_exceeded && output)
      parsed = validateOutput(JSON.parse(output.bytes.toString()), attempt, current.observation, current.manifest.target.receptor_sha256, current.manifest.scope.minimum_accepted_candidates, current.prepared)
    else error ??= "Registered scientific evaluator did not produce a successful bounded result"
  } catch (failure) {
    invalidOutput = failure instanceof Error ? failure.message : String(failure)
    error = "Scientific output failed integrity validation: " + invalidOutput
  }
  const { schema: _, id: _id, classification: _class, missing_evidence: _missing, evidence_refs: _refs, new_compute: _new, ...source } = current.observation
  const aggregate = (ids: string[]): "pass" | "fail" | "unknown" => {
    if (!parsed) return "unknown"
    const states = parsed.result.candidates.map(item => combined(ids.map(id => item.checks.find(check => check.id === id) ?? { status: "unknown" })))
    return states.filter(state => state === "pass").length >= current.manifest.scope.minimum_accepted_candidates ? "pass" : states.some(state => state === "unknown") ? "unknown" : "fail"
  }
  const states = parsed ? [parsed.result.receptor.status === "pass" ? "pass" : "unknown", aggregate(parsed.chemistry), aggregate(parsed.geometry),
    parsed.result.outcome === "objective_met" ? "pass" : parsed.result.outcome === "no_viable_candidate" ? "fail" : "unknown"] as const : ["unknown", "unknown", "unknown", "unknown"] as const
  const value = createResearchObservation({ ...source, artifacts,
    integrity_errors: [...source.integrity_errors, ...(invalidOutput ? ["invalid_host_scientific_output:" + invalidOutput] : [])],
    failure: !parsed && !invalidOutput && stderr ? { category: "environment", detail: "Local scientific evaluator failed; the original native engine status is unchanged: " + error, evidence_refs: [stderrRef] } : source.failure,
    candidates: source.candidates.map(candidate => ({ ...candidate, scientific_status: parsed?.result.candidates.find(item => item.compound_id === candidate.id)?.status ?? "unknown" })),
    counts: { ...source.counts, scientific_passed: parsed ? parsed.result.counts.pass : null },
    scientific_checks: required.map((id, index) => ({ id, status: states[index]!, evaluator: current.manifest.evaluator.version, evidence_refs: output ? [ref] : [],
      detail: parsed ? "Host-executed registered evaluator, bound to this exact native task, candidate denominator and artifact snapshot; computational shortlist only." : error! })),
    limitations: [...source.limitations.filter(item => !item.startsWith("Scientific evaluation has not run")),
      "Local scientific postprocessing is new; native task identity and remote execution provenance remain unchanged.",
      "Remote compute billing remains unsettled unless a separate trusted resource receipt exists.", ...(parsed?.result.limitations ?? [error!])] })
  return input.research.db.transaction(() => {
    input.research.settle(input.id, attempt.key, measured, usageRow!.key)
    input.research.record(input.id, `native-observation-binding:${value.id}`, { schema: "drug-design-native-observation-binding-v1", observation_id: value.id,
      decision_id: attempt.decision_id, receipt_ref: attempt.source_receipt_ref, task_id: attempt.task_id, plan_digest: attempt.plan_digest,
      scientific_process_receipt_ref: row.key, source_observation_id: attempt.source_observation_id, evaluation_attempt_digest: researchDigest(attempt) })
    input.research.record(input.id, `evaluation-completed:${attempt.key}`, { schema: "drug-design-native-scientific-completion-v1",
      observation_id: value.id, process_receipt_ref: row.key, output_sha256: receipt.output?.sha256 ?? null, evaluator_failed: !parsed })
    const loop = input.research.observe(input.id, value.id, value)
    return { status: "completed" as const, evaluation_ref: output ? ref : null, observation: value, receipt_ref: attempt.source_receipt_ref, reservation_key: attempt.key, loop }
  }).immediate()
}

async function recover(input: Identity, current: Extract<Awaited<ReturnType<typeof context>>, { ready: true }>, key: string) {
  const row = input.research.evidence(input.id, "host").find(row => row.key === `evaluation-attempt:${key}`)
  if (!row) return null
  const attempt = attemptSchema.parse(row.payload)
  if (attempt.key !== key || attempt.source_receipt_digest !== receiptDigest(current.captured.receipt)
    || attempt.source_observation_id !== current.observation.id || attempt.contract_digest !== current.action.loop.digest)
    throw Error("Scientific evaluation claim belongs to different native evidence")
  const reservation = input.research.reservations(input.id).find(row => row.key === key)
  if (!reservation || reservation.payload.kind !== "evaluation" || reservation.payload.plan_digest !== researchDigest(attempt)
    || reservation.payload.resources.cpu_core_hours !== attempt.cpu_seconds / 3600) throw Error("Scientific attempt lacks its exact resource reservation")
  return finish(input, current, attempt)
}
const keyFor = (current: Extract<Awaited<ReturnType<typeof context>>, { ready: true }>) => "evaluation-" + researchDigest({
  task: current.captured.receipt.task_id, plan: current.captured.receipt.plan_digest, receipt: receiptDigest(current.captured.receipt), registry: current.registry })

/** Recovery performs verified local reads only; a claimed attempt with no host
 * wait4 resource receipt retains its reservation and can never rerun implicitly. */
export async function recoverResearchNativeEvaluation(input: Identity) {
  input.abort?.throwIfAborted()
  const current = await context(input)
  if (!current.ready) return { status: "not_ready" as const, reason: current.reason, observation: null, loop: current.captured.loop }
  return await recover(input, current, keyFor(current)) ?? { status: "not_ready" as const, reason: "No scientific evaluation has been claimed", observation: null, loop: input.research.get(input.id) }
}

/** Host-only action; arguments contain native/store identity, never paths,
 * candidate values, arbitrary programs, scientific judgments or model replies. */
export async function evaluateResearchNativeAction(input: Identity) {
  input.abort?.throwIfAborted()
  const current = await context(input)
  if (!current.ready) return { status: "not_ready" as const, reason: current.reason, observation: null, loop: current.captured.loop }
  const key = keyFor(current), prior = await recover(input, current, key)
  if (prior) return prior
  const registry = current.registry
  const now = Date.now(), loop = input.research.get(input.id)
  const wall = Math.min(registry.wall_ms, loop.created + loop.contract.limits.max_elapsed_ms - now)
  if (wall < 1000 || ["BLOCKED", "FINISHED", "CANCELLED"].includes(loop.state)) throw Error("Research state/deadline prevents scientific evaluation")
  const directory = contained(current.root, path.join(".research-evaluations", input.id, key))
  const attempt = attemptSchema.parse({ schema: "drug-design-host-scientific-attempt-v1", key, task_id: current.captured.receipt.task_id,
    plan_digest: current.captured.receipt.plan_digest, contract_digest: loop.digest, decision_id: current.captured.decision_id,
    source_observation_id: current.observation.id, source_receipt_ref: current.captured.receipt_ref, source_receipt_digest: receiptDigest(current.captured.receipt),
    manifest_sha256: registry.manifest_sha256, evaluator_sha256: registry.evaluator_sha256, interpreter_sha256: registry.interpreter_sha256,
    wrapper_sha256: sha(wrapper), directory, inputs: current.inputs, artifacts: current.artifacts, cpu_seconds: registry.cpu_seconds, wall_ms: wall, created: now })
  const claimed = input.research.db.transaction(() => {
    if (input.research.evidence(input.id, "host").some(row => row.key === `evaluation-attempt:${key}`)) return false
    input.research.reserve(input.id, input.research.get(input.id).revision, { key, kind: "evaluation", round: 0, candidate_slots: 0,
      resources: { ...emptyResources(), cpu_core_hours: registry.cpu_seconds / 3600 }, plan_digest: researchDigest(attempt), source_release: loop.contract.inputs.source_release })
    input.research.record(input.id, `evaluation-attempt:${key}`, attempt)
    input.research.transition(input.id, input.research.get(input.id).revision, "EVALUATING", { reason: "registered_local_scientific_evaluation", key })
    return true
  }).immediate()
  if (!claimed) return (await recover(input, current, key))!
  // Everything after the durable claim is at-most-once. A process crash or an
  // uncertain spawn retains the full reservation for explicit reconciliation.
  try {
    await mkdir(path.dirname(directory), { recursive: true, mode: 0o700 })
    await mkdir(directory, { recursive: false, mode: 0o700 })
    if (await realpath(directory) !== directory) throw Error("Scientific output directory was redirected")
    for (const [role, relative] of Object.entries(roles)) {
      const source = attempt.artifacts[role]!, value = await boundedFile(source.path, source.sha256)
      const filename = path.join(directory, "artifacts", relative)
      await mkdir(path.dirname(filename), { recursive: true, mode: 0o700 })
      await writeFile(filename, value.bytes, { flag: "wx", mode: 0o600 })
    }
    const output = path.join(directory, "evaluation.json")
    const snapshotInputs = Object.entries(roles).map(([role, relative]) => ({ path: path.join(directory, "artifacts", relative), sha256: attempt.artifacts[role]!.sha256 }))
    const config = { python_version: registry.python_version, cpu_seconds: attempt.cpu_seconds, inputs: [...attempt.inputs, ...snapshotInputs],
      script: contained(current.root, registry.evaluator), manifest: contained(current.root, registry.manifest),
      receptor: contained(current.root, current.manifest.target.receptor_path), artifacts: path.join(directory, "artifacts"), output }
    input.abort?.throwIfAborted()
    const remaining = Math.min(attempt.wall_ms, loop.created + loop.contract.limits.max_elapsed_ms - Date.now())
    if (remaining <= 0) throw Error("Research deadline expired before scientific process start")
    if (sha(await readFile(await realpath(current.interpreter))) !== registry.interpreter_sha256) throw Error("Registered interpreter changed before spawn")
    const start = performance.now()
    const process = Bun.spawn([current.interpreter, "-I", "-c", wrapper, JSON.stringify(config)], { cwd: directory, stdin: "ignore", stdout: "pipe", stderr: "pipe",
      timeout: remaining, signal: input.abort, killSignal: "SIGKILL", env: { PATH: "/usr/bin:/bin", LANG: "C.UTF-8", PYTHONNOUSERSITE: "1",
        OMP_NUM_THREADS: "1", OPENBLAS_NUM_THREADS: "1", MKL_NUM_THREADS: "1", VECLIB_MAXIMUM_THREADS: "1", NUMEXPR_NUM_THREADS: "1" } })
    let exceeded = false
    const collect = async (stream: ReadableStream<Uint8Array>) => {
      const reader = stream.getReader(), chunks: Uint8Array[] = []; let bytes = 0
      while (true) {
        const item = await reader.read()
        if (item.done) break
        bytes += item.value.length
        if (bytes > 256 * 1024) { exceeded = true; process.kill("SIGKILL") }
        else chunks.push(item.value)
      }
      return Buffer.concat(chunks)
    }
    const [exit, stdout, stderr] = await Promise.all([process.exited, collect(process.stdout), collect(process.stderr)])
    const usage = process.resourceUsage()
    const microseconds = (value: unknown) => {
      const number = typeof value === "bigint" && value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : value
      if (typeof number !== "number" || !Number.isSafeInteger(number) || number < 0) throw Error("Complete host process CPU receipt unavailable")
      return number
    }
    if (!usage) throw Error("Complete host process CPU receipt unavailable")
    const userCpu = microseconds(usage.cpuTime.user), systemCpu = microseconds(usage.cpuTime.system)
    if (microseconds(usage.cpuTime.total) !== userCpu + systemCpu) throw Error("Host CPU receipt total is inconsistent")
    // Persist measured usage before any fallible filesystem work. A crash while
    // saving logs/results must never erase an already observed wait4 receipt.
    const resourceReceipt = resourceReceiptSchema.parse({ schema: "drug-design-host-scientific-resource-receipt-v1",
      attempt_digest: researchDigest(attempt), source_receipt_digest: attempt.source_receipt_digest,
      pid: process.pid, exit_code: exit, signal: process.signalCode ?? null, elapsed_ms: performance.now() - start,
      cpu_user_microseconds: userCpu, cpu_system_microseconds: systemCpu,
      usage_origin: "host_Bun_Subprocess_resourceUsage_wait4", output_limit_exceeded: exceeded })
    const resourceRef = `evaluation-resource:${key}`
    input.research.record(input.id, resourceRef, resourceReceipt)
    const fileErrors: string[] = []
    const streams = await Promise.all(([['stdout.txt', stdout], ['stderr.txt', stderr]] as const).map(async ([name, bytes]) => {
      try {
        const filename = path.join(directory, name); await writeFile(filename, bytes, { flag: "wx", mode: 0o600 }); return { path: filename, sha256: sha(bytes) }
      } catch (error) { fileErrors.push(`${name}: ${error instanceof Error ? error.message : String(error)}`); return null }
    }))
    let result: { path: string; sha256: string } | null = null
    try {
      if (await Bun.file(output).exists()) { const value = await boundedFile(output, undefined, 8 * 1024 * 1024); result = { path: value.path, sha256: value.sha256 } }
    } catch (error) { fileErrors.push(`evaluation.json: ${error instanceof Error ? error.message : String(error)}`) }
    const receipt = receiptSchema.parse({ schema: "drug-design-host-scientific-process-receipt-v1", attempt_digest: researchDigest(attempt),
      source_receipt_digest: attempt.source_receipt_digest, evaluator_sha256: registry.evaluator_sha256, manifest_sha256: registry.manifest_sha256,
      wrapper_sha256: sha(wrapper), interpreter_sha256: registry.interpreter_sha256, pid: process.pid, exit_code: exit, signal: process.signalCode ?? null,
      elapsed_ms: resourceReceipt.elapsed_ms, cpu_user_microseconds: userCpu, cpu_system_microseconds: systemCpu,
      usage_origin: "host_Bun_Subprocess_resourceUsage_wait4", resource_receipt_ref: resourceRef, resource_receipt_digest: researchDigest(resourceReceipt),
      output: result, stdout: streams[0], stderr: streams[1], output_limit_exceeded: exceeded,
      error: fileErrors.length ? "Scientific evidence file persistence failed: " + fileErrors.sort().join("; ") : null })
    input.research.record(input.id, `evaluation-process:${key}`, receipt)
    return await finish(input, current, attempt)
  } catch (error) {
    input.research.record(input.id, `evaluation-interrupted:${key}:${researchDigest(String(error))}`, { schema: "drug-design-scientific-evaluation-interruption-v1",
      key, error: error instanceof Error ? error.message : String(error), automatic_retry_permitted: false, reservation_released: false })
    return { status: "needs_reconciliation" as const, reason: error instanceof Error ? error.message : String(error), evaluation_ref: null,
      observation: null, receipt_ref: attempt.source_receipt_ref, reservation_key: key, loop: input.research.get(input.id) }
  }
}
