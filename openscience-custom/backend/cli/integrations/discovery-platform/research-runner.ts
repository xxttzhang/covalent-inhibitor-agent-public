import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, mkdir, readFile, realpath, rename, writeFile } from "node:fs/promises"
import { parseArgs } from "node:util"
import { researchContract, researchDigest, researchHash, exceedsResearchLimit } from "./research-loop-contract"
import { ResearchLoopStore } from "./research-loop-store"
import { captureResearchArtifact, verifyResearchObservation } from "./research-observations"
import { researchSnapshot } from "./research-loop"
import { readResearchModelCall, ResearchModelBudgetPolicy, researchModelCost } from "./research-model-budget"
import { recoverResearchLiveDecision, runResearchLiveDecision } from "./research-live"
import { preparedReceptorChecks, readPreparedReceptorEvidence } from "./research-prepared-receptor"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"

const file = z.strictObject({ path: z.string().startsWith("/"), sha256: researchHash })
const evidence = file.extend({ ref: z.string().min(1) })
export const researchRunnerSpec = z.strictObject({
  schema: z.literal("research-host-runner-spec-v1"), directory: z.string().startsWith("/"),
  contract: researchContract,
  model: z.strictObject({ provider_id: z.string().min(1), model_id: z.string().min(1), agent: z.literal("research") }),
  policy: ResearchModelBudgetPolicy, policy_verification: file, policy_evidence: z.array(evidence).min(1),
  historical: file, receptor_audit: file,
  scientific_evidence: z.strictObject({ manifest: file, binding: file, diagnostic: file }).optional(),
  prepared_receptor_evidence: z.strictObject({ manifest: file, binding: file }).optional(),
  limits: z.strictObject({ max_output_tokens: z.number().int().positive(), timeout_ms: z.number().int().positive().max(2_147_483_647) }),
})
export type ResearchRunnerSpec = z.infer<typeof researchRunnerSpec>

const verification = z.strictObject({ schema: z.literal("research-host-policy-verification-v1"),
  policy_digest: researchHash, status: z.literal("verified"), evidence_refs: z.array(z.string().min(1)).min(1),
  reviewed_at: z.string().datetime({ offset: true }), review_basis: z.string().min(1) })
const authorization = z.strictObject({ schema: z.literal("research-host-existing-goal-authorization-v1"),
  origin: z.literal("existing-user-goal"), scope: z.literal("offline-replay-model-decisions"),
  loop_id: z.string().min(1), contract_digest: researchHash, policy_digest: researchHash,
  source_ref: z.string().min(1), statement: z.string().min(1), authorized_at: z.string().datetime({ offset: true }),
  max_calls: z.number().int().positive(), max_tokens: z.number().int().positive(), max_cost_usd: z.number().finite().positive().nullable() })
const auditSchema = z.object({ schema: z.literal("drug-design-receptor-audit-v1"), evaluator: z.string().min(1),
  evaluator_sha256: researchHash, structure_sha256: researchHash, entry_id: z.literal("1M17"), uniprot: z.literal("P00533"),
  uniprot_residue: z.literal(797), auth_asym_id: z.literal("A"), author_residue_id: z.literal("773"),
  reactive_atom: z.literal("SG"), taxon: z.literal(9606), mapping_status: z.enum(["pass", "fail", "unknown"]),
  status: z.enum(["pass", "fail", "unknown"]), checks: z.array(z.unknown()).min(1), new_compute: z.literal(false),
}).passthrough()
const manifestSchema = z.strictObject({ schema: z.literal("research-host-runner-v1"), directory: z.string(), run_directory: z.string(),
  loop_id: z.string(), contract_digest: researchHash, session_id: z.string().startsWith("ses_"),
  user_message_id: z.string(), user_part_id: z.string(), user_digest: researchHash, part_digest: researchHash,
  spec_digest: researchHash, created_at: z.number().int(), default_key: z.literal("decision-1"),
  new_remote_compute: z.literal(false), native_approval_origin: z.literal(false) })
type Manifest = z.infer<typeof manifestSchema>
const sha = (bytes: string | Uint8Array) => createHash("sha256").update(bytes).digest("hex")

async function readBoundFile(input: z.infer<typeof file>) {
  const filename = path.resolve(input.path)
  if (await realpath(filename) !== filename) throw Error("Runner evidence must not be redirected by a symlink")
  const stat = await lstat(filename)
  if (!stat.isFile() || stat.size > 32 * 1024 * 1024) throw Error("Runner evidence must be a bounded regular JSON file")
  const bytes = await readFile(filename)
  if (sha(bytes) !== input.sha256) throw Error("Runner evidence file changed: " + filename)
  return { filename, bytes, value: JSON.parse(bytes.toString("utf8")) as unknown }
}

const scientificChecks = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility", "pose_geometry", "registered_objective_acceptance"]
const scientificManifest = z.object({ schema: z.literal("drug-design-scientific-manifest-v2"),
  version: z.literal("egfr-c797-library-computational-shortlist-v2.1"),
  target: z.object({ name: z.literal("EGFR"), entry: z.literal("1M17"), uniprot: z.literal("P00533"), chain: z.literal("A"),
    canonical_residue: z.literal(797), author_residue: z.literal(773), residue: z.literal("CYS"), atom: z.literal("SG"),
    receptor_path: z.string(), receptor_sha256: researchHash, mapping_path: z.string(), mapping_sha256: researchHash }).passthrough(),
  evaluator: z.object({ version: z.literal("egfr-library-scientific-evaluator-v2.1"), path: z.string(), sha256: researchHash }).passthrough(),
  geometry: z.record(z.string(), z.number().finite()), required_receptor_checks: z.array(z.string()), required_scientific_checks: z.array(z.string()),
  prepared_receptor_semantics: z.object({ method_revision: z.literal("2.1"), thresholds_changed_from_v1: z.literal(false),
    candidate_chemistry_and_geometry_core_sha256: researchHash }).passthrough(),
}).passthrough()
const atomIdentity = z.tuple([z.string(), z.number().int(), z.string(), z.string()])
const stereoViolation = z.object({ atom: atomIdentity, expected: z.string(), actual: z.string().nullable() }).strict()
const pairViolation = z.object({ atoms: z.tuple([atomIdentity, atomIdentity]), distance_angstrom: z.number().finite(),
  bounds: z.tuple([z.number().finite(), z.number().finite()]), involves_modeled_atom: z.boolean() }).strict()
const provenanceAtoms = z.object({ atoms: z.array(z.object({ identity: atomIdentity, element: z.string(), coordinate_origin: z.string() }).passthrough()) }).passthrough()
const topologySchema = z.object({ atoms: z.array(z.object({ chain: z.string(), number: z.number().int(), residue: z.string(), name: z.string() }).passthrough()),
  bonds: z.array(z.tuple([z.number().int().nonnegative(), z.number().int().nonnegative()])) }).passthrough()
const diagnosticSchema = z.object({ schema: z.literal("drug-design-prepared-receptor-failure-diagnostic-v2"), method_revision: z.literal("2.1"),
  purpose: z.literal("diagnostic_only_not_new_acceptance"), audit_sha256: researchHash,
  classes: z.array(z.record(z.string(), z.unknown())) }).passthrough()

/** Read actual failed science as evidence, without changing the old contract or
 * promoting a prepared proposal to a native plan. No evaluator is executed. */
async function readScientificEvidenceV2(spec: ResearchRunnerSpec) {
  const input = spec.scientific_evidence
  if (!input) return []
  for (const item of Object.values(input))
    if (!path.resolve(item.path).startsWith(spec.directory + path.sep)) throw Error("Scientific context must be contained in its project")
  const [manifestFile, bindingFile, diagnosticFile] = await Promise.all([
    readBoundFile(input.manifest), readBoundFile(input.binding), readBoundFile(input.diagnostic),
  ])
  const manifest = scientificManifest.parse(manifestFile.value)
  if (researchDigest(manifest.required_scientific_checks) !== researchDigest(scientificChecks)
    || researchDigest(manifest.required_receptor_checks) !== researchDigest(preparedReceptorChecks)
    || path.resolve(spec.directory, manifest.target.mapping_path) !== bindingFile.filename
    || manifest.target.mapping_sha256 !== input.binding.sha256)
    throw Error("Scientific manifest does not bind the exact context proof and required checks")
  // This target view is only an argument to read-only proof validation. The
  // persisted historical/offline contract remains byte-for-byte unchanged.
  const proposedTarget = { ...spec.contract, target: { ...spec.contract.target,
    receptor_sha256: manifest.target.receptor_sha256, mapping_sha256: input.binding.sha256 } }
  const proof = await readPreparedReceptorEvidence({ root: spec.directory, value: bindingFile.value, contract: proposedTarget,
    geometry: manifest.geometry, evaluator: manifest.evaluator, receptor_path: manifest.target.receptor_path,
    required_checks: manifest.required_receptor_checks, core_sha256: manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 })
  if (proof.source_sha256 !== spec.contract.target.receptor_sha256
    || proof.references.source_mapping_audit.sha256 !== spec.receptor_audit.sha256
    || path.resolve(spec.directory, proof.references.source_mapping_audit.path) !== path.resolve(spec.receptor_audit.path))
    throw Error("Prepared proposal does not descend from the runner's original target audit")
  const diagnostic = diagnosticSchema.parse(diagnosticFile.value)
  if (diagnostic.audit_sha256 !== proof.audit_sha256) throw Error("Failure diagnostic refers to a different prepared audit")
  const atomFile = await readBoundFile({ path: path.resolve(spec.directory, proof.references.atom_mapping.path), sha256: proof.references.atom_mapping.sha256 })
  const receiptFile = await readBoundFile({ path: path.resolve(spec.directory, proof.references.preparation_receipt.path), sha256: proof.references.preparation_receipt.sha256 })
  const receipt = z.object({ artifacts: z.record(z.string(), researchHash) }).passthrough().parse(receiptFile.value)
  const topology = topologySchema.parse((await readBoundFile({ path: path.join(path.dirname(receiptFile.filename), "topology.json"),
    sha256: receipt.artifacts["topology.json"]! })).value)
  const atoms = provenanceAtoms.parse(atomFile.value).atoms
  const atomKey = (value: z.infer<typeof atomIdentity>) => JSON.stringify(value)
  const map = new Map(atoms.map(atom => [atomKey(atom.identity), atom]))
  if (map.size !== atoms.length) throw Error("Duplicate atom provenance in diagnostic context")
  const keys = topology.atoms.map(atom => [atom.chain, atom.number, atom.residue, atom.name] as z.infer<typeof atomIdentity>)
  const neighbors = new Map<string, z.infer<typeof atomIdentity>[]>()
  for (const [first, second] of topology.bonds) {
    if (!keys[first] || !keys[second]) throw Error("Diagnostic topology bond index is invalid")
    for (const [a, b] of [[keys[first]!, keys[second]!], [keys[second]!, keys[first]!] ]) {
      const list = neighbors.get(atomKey(a)) ?? []
      list.push(b); neighbors.set(atomKey(a), list)
    }
  }
  const atom = (key: z.infer<typeof atomIdentity>) => {
    const value = map.get(atomKey(key))
    if (!value) throw Error("Diagnostic atom identity is absent from preparation provenance")
    return value
  }
  const modeled = (key: z.infer<typeof atomIdentity>) => atom(key).coordinate_origin !== "retained_observed_coordinate"
  const classes = proof.audit.checks.filter(item => item.status !== "pass").map(item => {
    const measurements = item.measurements
    if (item.id === "standard_amino_acid_stereochemistry") {
      const violations = z.array(stereoViolation).parse(measurements.violations).map(value => {
        const heavy = (neighbors.get(atomKey(value.atom)) ?? []).filter(key => atom(key).element !== "H")
        if (!heavy.length) throw Error("Stereocenter diagnostic lacks its molecular neighborhood")
        return { ...value, center_origin: atom(value.atom).coordinate_origin,
          modeled_heavy_neighbors: heavy.filter(modeled), center_or_heavy_neighbor_modeled: modeled(value.atom) || heavy.some(modeled) }
      })
      return { id: item.id, violation_count: violations.length,
        modeled_centers: violations.filter(value => value.center_origin !== "retained_observed_coordinate").length,
        retained_centers: violations.filter(value => value.center_origin === "retained_observed_coordinate").length,
        entirely_retained_local_environments: violations.filter(value => !value.center_or_heavy_neighbor_modeled).length, violations }
    }
    if (!["intramolecular_distance_angles", "intramolecular_distance_internal"].includes(item.id))
      throw Error("Prepared failure diagnostic requires a supported explicit classification")
    const violations = z.array(pairViolation).parse(measurements.first_100_violations)
    if (violations.length !== measurements.violation_count) throw Error("Failure diagnostic cannot summarize an incomplete violation denominator")
    const counts = new Map<string, { residue: (string | number)[]; violation_involvements: number }>()
    for (const value of violations) {
      if (value.involves_modeled_atom !== value.atoms.some(modeled)) throw Error("Pair failure model origin contradicts atom provenance")
      for (const key of new Set(value.atoms.map(key => JSON.stringify(key.slice(0, 3))))) {
        const row = counts.get(key) ?? { residue: JSON.parse(key), violation_involvements: 0 }
        row.violation_involvements++; counts.set(key, row)
      }
    }
    const modeledCount = violations.filter(value => value.involves_modeled_atom).length
    if (measurements.modeled_pair_violations !== modeledCount || measurements.retained_observed_pair_violations !== violations.length - modeledCount)
      throw Error("Pair failure counts contradict atom provenance")
    return { id: item.id, violation_count: violations.length, modeled_pair_violations: modeledCount,
      retained_observed_pair_violations: violations.length - modeledCount,
      residue_involvement_counts: [...counts.values()].sort((a, b) => String(a.residue[0]).localeCompare(String(b.residue[0])) || Number(a.residue[1]) - Number(b.residue[1]) || String(a.residue[2]).localeCompare(String(b.residue[2]))),
      count_note: "One pair may involve two residues; residue involvement counts must not be summed as unique pair counts." }
  })
  if (researchDigest(classes) !== researchDigest(diagnostic.classes)) throw Error("Failure diagnostic contradicts independently derived audit classification")
  const provenance = { origin: "local_cpu_preparation_and_independent_audit", new_remote_compute: false,
    native_execution: false, modeled_coordinates_are_experimental: false, source_sha256: proof.source_sha256,
    prepared_sha256: proof.prepared_sha256, binding_sha256: input.binding.sha256, audit_sha256: proof.audit_sha256 }
  return [
    { key: "runner-prepared-receptor-v2.1", payload: { schema: "research-runner-prepared-receptor-evidence-v2", ...provenance,
      manifest: input.manifest, binding: input.binding, audit: proof.references.independent_audit,
      status: proof.audit.status, scientific_passed: proof.scientific_passed, native_admissible: proof.admissible,
      audit_input_binding_complete: proof.audit_input_binding_complete, limitations: proof.limitations, status_preserved: true,
      checks: proof.audit.checks.map(item => ({ id: item.id, status: item.status,
        measurements: Object.fromEntries(Object.entries(item.measurements).filter(([key]) => ["checked", "tested", "violation_count", "modeled_pair_violations", "retained_observed_pair_violations"].includes(key))),
        representative_violations: Array.isArray(item.measurements.first_100_violations) ? item.measurements.first_100_violations.slice(0, 3) : [] })),
      runtime: proof.audit.runtime, immutable_historical_contract_preserved: true } },
    { key: "runner-prepared-receptor-failures-v2.1", payload: { schema: "research-runner-prepared-receptor-failures-v2", ...provenance,
      diagnostic: input.diagnostic, diagnostic_only: true, classes: classes.map(item => "violations" in item ? { ...item,
        violations: item.violations!.map(value => ({ atom: value.atom, expected: value.expected, actual: value.actual,
          center_origin: value.center_origin, modeled_heavy_neighbor_count: value.modeled_heavy_neighbors.length,
          center_or_heavy_neighbor_modeled: value.center_or_heavy_neighbor_modeled })) } : item) } },
  ]
}

/** Preserve historical receptor/candidate conclusions. A newly audited input
 * is prospective evidence, not a retrospective reevaluation of old poses. */
async function readScientificEvidenceV4(spec: ResearchRunnerSpec) {
  const input = spec.prepared_receptor_evidence
  if (!input) return []
  if (input.manifest.path !== path.join(spec.directory, preparedReceptorV4.manifest.path)
    || input.manifest.sha256 !== preparedReceptorV4.manifest.sha256
    || input.binding.path !== path.join(spec.directory, preparedReceptorV4.binding.path)
    || input.binding.sha256 !== preparedReceptorV4.binding.sha256) throw Error("Runner prepared receptor must use the registered v4 evidence")
  const [manifestFile, bindingFile] = await Promise.all([readBoundFile(input.manifest), readBoundFile(input.binding)])
  const manifest = scientificManifest.extend({ schema: z.literal("drug-design-scientific-manifest-v4"),
    version: z.literal("egfr-c797-library-computational-shortlist-v4.0"),
    evaluator: scientificManifest.shape.evaluator.extend({ version: z.literal("egfr-library-scientific-evaluator-v4.0") }),
    prepared_receptor_semantics: scientificManifest.shape.prepared_receptor_semantics.extend({ method_revision: z.literal("4.0") }),
  }).parse(manifestFile.value)
  const proposed = { ...spec.contract, target: { ...spec.contract.target, receptor_sha256: manifest.target.receptor_sha256,
    mapping_sha256: input.binding.sha256 }, quality: { ...spec.contract.quality, manifest_sha256: input.manifest.sha256 } }
  const proof = await readPreparedReceptorEvidence({ root: spec.directory, value: bindingFile.value, contract: proposed,
    geometry: manifest.geometry, evaluator: manifest.evaluator, receptor_path: manifest.target.receptor_path,
    required_checks: manifest.required_receptor_checks, core_sha256: manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 })
  if (proof.source_sha256 !== spec.contract.target.receptor_sha256 || proof.references.source_mapping_audit.sha256 !== spec.receptor_audit.sha256
    || path.join(spec.directory, proof.references.source_mapping_audit.path) !== spec.receptor_audit.path)
    throw Error("V4 prepared proposal is not derived from the runner's historical target")
  return [{ key: "runner-prepared-receptor-v4.0", payload: { schema: "research-runner-prepared-receptor-evidence-v4",
    origin: "local_cpu_preparation_and_independent_audit", new_remote_compute: false, native_execution: false,
    historical_candidates_re_evaluated: false, immutable_historical_contract_preserved: true,
    scope: "Prospective receptor geometry only; historical candidates retain their original unknown scientific status.",
    manifest: input.manifest, binding: input.binding, audit: proof.references.independent_audit,
    source_sha256: proof.source_sha256, prepared_sha256: proof.prepared_sha256,
    status: proof.audit.status, scientific_passed: proof.scientific_passed, audit_input_binding_complete: proof.audit_input_binding_complete,
    prospective_scientific_input_admissible: proof.admissible, native_execution_authorized: false,
    checks: proof.audit.checks.map(item => ({ id: item.id, status: item.status })),
    runtime: proof.audit.runtime, limitations: proof.limitations } }]
}

async function readScientificEvidence(spec: ResearchRunnerSpec) {
  return [...await readScientificEvidenceV2(spec), ...await readScientificEvidenceV4(spec)]
}

async function atomic(filename: string, value: unknown) {
  const temporary = `${filename}.tmp-${process.pid}`
  await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { flag: "wx", mode: 0o600 })
  await rename(temporary, filename)
}

async function instance<T>(directory: string, fn: () => Promise<T>) {
  const { Instance } = await import("../../src/project/instance")
  // No InstanceBootstrap: preparing or recovering must not start watchers,
  // MCP servers, kernels or tools. Provider/LLM initialize only on explicit invoke.
  return Instance.provide({ directory, fn: async () => {
    try { return await fn() }
    finally { await Instance.dispose() }
  } })
}

/** Local preparation only. It creates a real platform session, but the host
 * request is explicitly marked as generated and is never a native approval
 * origin. The immutable research contract forbids all scientific computation. */
export async function prepareResearchRunner(input: { run: string; spec: ResearchRunnerSpec }) {
  const spec = researchRunnerSpec.parse(input.spec)
  const directory = await realpath(spec.directory)
  if (directory !== spec.directory || spec.contract.directory !== directory) throw Error("Runner directory differs from its contract")
  if (spec.contract.mode !== "offline-replay" || spec.contract.limits.resources.gpu_hours !== 0
    || spec.contract.limits.resources.cpu_core_hours !== 0 || spec.contract.limits.resources.compute_points !== 0
    || spec.contract.allowed_adaptations.some(action => action !== "inspect" && action !== "stop"))
    throw Error("This runner permits bounded offline evidence decisions only; scientific compute must remain zero")
  if (spec.model.provider_id !== spec.policy.provider_id || spec.model.model_id !== spec.policy.model_id
    || spec.limits.max_output_tokens > spec.policy.output_token_cap)
    throw Error("Runner model/output limit differs from its verified policy")
  const tokens = spec.policy.input_token_cap + spec.limits.max_output_tokens
  const cost = researchModelCost(spec.policy.input_token_cap, spec.limits.max_output_tokens, spec.policy)
  if ((cost === null) !== (spec.contract.limits.resources.model_cost === null))
    throw Error("Unbounded pricing requires an explicitly uncapped contract")
  if (tokens > spec.contract.limits.resources.model_tokens || exceedsResearchLimit(cost ?? 0, spec.contract.limits.resources.model_cost)
    || spec.limits.timeout_ms > spec.contract.limits.max_elapsed_ms || Date.parse(spec.policy.pricing.valid_until) <= Date.now())
    throw Error("Contract cannot reserve one complete verified model request within its token, cost or time limits")
  const run = path.resolve(input.run)
  if (!run.startsWith(directory + path.sep)) throw Error("Runner output must be contained in its project directory")
  if (await Bun.file(path.join(run, "runner.json")).exists()) {
    const existing = await load(run)
    try {
      if (existing.manifest.spec_digest !== researchDigest(spec)) throw Error("Runner directory already binds another frozen specification")
      for (const item of await readScientificEvidence(spec)) {
        const saved = existing.store.evidence(existing.manifest.loop_id, "host").find(row => row.key === item.key)
        if (!saved || researchDigest(saved.payload) !== researchDigest(item.payload))
          throw Error("Scientific evidence no longer matches the runner's frozen model context")
      }
      return { manifest: existing.manifest, resumed: true, model_called: false, new_remote_compute: false }
    } finally { existing.store.close() }
  }
  const [history, audit, reviewed, sources] = await Promise.all([
    readBoundFile(spec.historical), readBoundFile(spec.receptor_audit), readBoundFile(spec.policy_verification),
    Promise.all(spec.policy_evidence.map(readBoundFile)),
  ])
  const source = z.object({ schema: z.literal("drug-design-historical-replay-import-v1"), observation: z.unknown(), contract: researchContract }).passthrough().parse(history.value)
  const observation = verifyResearchObservation(source.observation)
  if (observation.origin !== "historical_import" || observation.new_compute || observation.run.task_id !== null)
    throw Error("Runner accepts verified historical observations without relabelling them as new/native execution")
  if (source.contract.target.gene !== spec.contract.target.gene || source.contract.target.canonical_site !== spec.contract.target.canonical_site
    || observation.run.release_id !== source.contract.inputs.source_release)
    throw Error("Historical observation has a mismatched source target or release")
  const artifacts = await Promise.all(observation.artifacts.map(item => {
    if (!item.sha256) throw Error("Historical evidence lacks a frozen content hash")
    return captureResearchArtifact({ path: item.path, ref: item.ref, expectedSha256: item.sha256 })
  }))
  if (!artifacts.length || artifacts.some(item => item.status !== "verified")) throw Error("Historical artifact bytes no longer match their observation")
  const receptor = auditSchema.parse(audit.value)
  if (receptor.structure_sha256 !== spec.contract.target.receptor_sha256
    || (spec.contract.target.mapping_sha256 !== null && spec.contract.target.mapping_sha256 !== spec.receptor_audit.sha256))
    throw Error("Receptor audit does not bind the new research contract's exact structure")
  const scientific = await readScientificEvidence(spec)
  const review = verification.parse(reviewed.value)
  const refs = spec.policy_evidence.map(item => item.ref)
  if (new Set(refs).size !== refs.length || refs.some(ref => ref.startsWith("runner-") || ref.startsWith("model-"))
    || researchDigest([...refs].sort()) !== researchDigest([...spec.policy.evidence_refs].sort())
    || researchDigest([...refs].sort()) !== researchDigest([...review.evidence_refs].sort())
    || review.policy_digest !== researchDigest(spec.policy) || Date.parse(review.reviewed_at) > Date.now())
    throw Error("Policy verification and evidence references do not match the exact reviewed policy")
  // Exclusive directory creation is the preparation claim. A crash before the
  // final manifest remains visible and cannot silently create another session.
  await mkdir(run, { recursive: false, mode: 0o700 })
  if (await realpath(run) !== run) throw Error("Runner output path is redirected")
  await writeFile(path.join(run, "prepare-claim.json"), JSON.stringify({ spec_digest: researchDigest(spec), state: "preparing" }), { flag: "wx", mode: 0o600 })
  await atomic(path.join(run, "spec.json"), spec)
  return instance(directory, async () => {
    const { Session } = await import("../../src/session")
    const { Identifier } = await import("../../src/id/id")
    const { Agent } = await import("../../src/agent/agent")
    if (!(await Agent.get(spec.model.agent))) throw Error("Configured research agent is unavailable")
    const session = await Session.create({ title: "Bounded drug-design evidence decision", permission: [{ permission: "*", pattern: "*", action: "deny" }] })
    await atomic(path.join(run, "session-created.json"), { session_id: session.id, directory, spec_digest: researchDigest(spec), model_called: false })
    const contract = researchContract.parse({ ...spec.contract, session: session.id })
    const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
      agent: spec.model.agent, effort: "normal", model: { providerID: spec.model.provider_id, modelID: spec.model.model_id }, time: { created: Date.now() } })
    const part = await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: user.id, type: "text", synthetic: true,
      text: `Host-initiated actual research invocation, pending explicit invoke under the existing user's authorized goal.\nObjective: ${contract.objective}\nOnly inspect recorded evidence or stop. Historical observations remain historical; no native compute approval is created.` })
    const store = new ResearchLoopStore(path.join(run, "research.sqlite"))
    try {
      const loop = store.db.transaction(() => {
        const loop = store.create(contract)
        store.observe(loop.id, observation.id, observation)
        store.record(loop.id, "runner-history-source", { schema: "research-runner-history-source-v1", path: history.filename,
          sha256: spec.historical.sha256, original_contract: source.contract, artifact_readback: artifacts, new_compute: false })
        store.record(loop.id, "runner-receptor-audit", { schema: "research-runner-receptor-audit-v1", path: audit.filename,
          sha256: spec.receptor_audit.sha256, audit: receptor, status_preserved: true, new_compute: false })
        for (const item of scientific) store.record(loop.id, item.key, item.payload)
        for (const [index, item] of spec.policy_evidence.entries()) store.record(loop.id, item.ref, {
          schema: "research-runner-policy-source-v1", path: item.path, sha256: item.sha256, content: sources[index]!.value,
        })
        store.record(loop.id, "runner-policy-verification", { schema: "research-runner-policy-verification-v1",
          path: reviewed.filename, sha256: spec.policy_verification.sha256, verification: review, policy: spec.policy })
        store.record(loop.id, "runner-request-origin", { schema: "research-runner-request-origin-v1", origin: "host-initiated-research-request",
          session_id: session.id, user_message_id: user.id, user_part_id: part.id, user_digest: researchDigest(user), part_digest: researchDigest(part),
          spec_digest: researchDigest(spec), native_approval_origin: false, model_called: false })
        return store.get(loop.id)
      }).immediate()
      const manifest = manifestSchema.parse({ schema: "research-host-runner-v1", directory, run_directory: run,
        loop_id: loop.id, contract_digest: loop.digest, session_id: session.id, user_message_id: user.id, user_part_id: part.id,
        user_digest: researchDigest(user), part_digest: researchDigest(part), spec_digest: researchDigest(spec),
        created_at: loop.created, default_key: "decision-1", new_remote_compute: false, native_approval_origin: false })
      await atomic(path.join(run, "runner.json"), manifest)
      return { manifest, resumed: false, model_called: false, new_remote_compute: false }
    } finally { store.close() }
  })
}

async function load(input: string) {
  const run = await realpath(input)
  const manifest = manifestSchema.parse(JSON.parse(await readFile(path.join(run, "runner.json"), "utf8")))
  const spec = researchRunnerSpec.parse(JSON.parse(await readFile(path.join(run, "spec.json"), "utf8")))
  if (manifest.run_directory !== run || manifest.directory !== spec.directory || manifest.spec_digest !== researchDigest(spec))
    throw Error("Runner manifest or specification binding changed")
  const database = path.join(run, "research.sqlite")
  if (await realpath(database) !== database || !(await lstat(database)).isFile()) throw Error("Runner database was redirected")
  const store = new ResearchLoopStore(database)
  try {
    const loop = store.get(manifest.loop_id)
    if (loop.digest !== manifest.contract_digest || loop.contract.session !== manifest.session_id
      || researchDigest(loop.contract) !== researchDigest({ ...spec.contract, session: manifest.session_id }))
      throw Error("Runner loop no longer matches its immutable manifest")
    const origin = store.evidence(loop.id, "host").find(row => row.key === "runner-request-origin")?.payload
    if (researchDigest(origin) !== researchDigest({ schema: "research-runner-request-origin-v1", origin: "host-initiated-research-request",
      session_id: manifest.session_id, user_message_id: manifest.user_message_id, user_part_id: manifest.user_part_id,
      user_digest: manifest.user_digest, part_digest: manifest.part_digest, spec_digest: manifest.spec_digest,
      native_approval_origin: false, model_called: false })) throw Error("Runner request/specification does not match its immutable host origin")
    return { manifest, spec, store }
  } catch (error) { store.close(); throw error }
}

async function session(manifest: Manifest, spec: ResearchRunnerSpec) {
  const { Session } = await import("../../src/session")
  const { MessageV2 } = await import("../../src/session/message-v2")
  const value = await Session.get(manifest.session_id)
  const message = await MessageV2.get({ sessionID: value.id, messageID: manifest.user_message_id })
  const user = MessageV2.User.parse(message.info)
  const part = message.parts.find(item => item.id === manifest.user_part_id)
  if (value.directory !== spec.directory || user.model.providerID !== spec.model.provider_id || user.model.modelID !== spec.model.model_id
    || user.agent !== spec.model.agent || researchDigest(user) !== manifest.user_digest || researchDigest(part) !== manifest.part_digest)
    throw Error("Runner session or host request no longer matches its saved context")
  return user
}

function report(value: Awaited<ReturnType<typeof load>>) {
  const snapshot = researchSnapshot(value.store, value.manifest.loop_id)
  return { schema: "research-host-runner-read-v1", manifest: value.manifest, loop: snapshot.loop,
    observations: snapshot.observations, host_evidence: value.store.evidence(snapshot.loop.id, "host"),
    decisions: value.store.evidence(snapshot.loop.id, "decision"),
    model_calls: value.store.reservations(snapshot.loop.id).filter(row => row.payload.kind === "model")
      .map(row => readResearchModelCall(value.store, snapshot.loop.id, row.key)), new_remote_compute: false }
}

export async function readResearchRunner(input: { run: string }) {
  const value = await load(input.run)
  try { return report(value) }
  finally { value.store.close() }
}

export async function recoverResearchRunner(input: { run: string; key?: string }) {
  const value = await load(input.run)
  try {
    return await instance(value.manifest.directory, async () => {
      await session(value.manifest, value.spec)
      const recovered = await recoverResearchLiveDecision({ store: value.store, id: value.manifest.loop_id, key: input.key ?? value.manifest.default_key })
      return { recovered, evidence: report(value) }
    })
  } finally { value.store.close() }
}

/** Explicit invocation only. Authorization records an already-granted user goal;
 * it is not a newly manufactured permission answer or native compute origin. */
export async function invokeResearchRunner(input: { run: string; key?: string; authorization: string; abort: AbortSignal }) {
  input.abort.throwIfAborted()
  const value = await load(input.run)
  try {
    return await instance(value.manifest.directory, async () => {
      const user = await session(value.manifest, value.spec)
      const identity = { store: value.store, id: value.manifest.loop_id, key: input.key ?? value.manifest.default_key }
      // Validate science even on idempotent invocation/recovery, before any
      // provider connection or new model reservation can be created.
      for (const item of await readScientificEvidence(value.spec)) {
        const saved = value.store.evidence(identity.id, "host").find(row => row.key === item.key)
        if (!saved || researchDigest(saved.payload) !== researchDigest(item.payload))
          throw Error("Scientific evidence no longer matches the runner's frozen model context")
      }
      const previous = await recoverResearchLiveDecision(identity)
      if (previous.status !== "no_attempt") return { result: previous, evidence: report(value) }
      const outstanding = value.store.reservations(identity.id).find(row => row.payload.kind === "model"
        && !value.store.evidence(identity.id, "decision").some(item => {
          const record = item.payload as { provenance?: { receipt_ref?: string } }
          return record.provenance?.receipt_ref === `model-receipt:${row.key}`
        }))
      if (outstanding) return { result: await recoverResearchLiveDecision({ ...identity, key: outstanding.key }), evidence: report(value) }
      const filename = await realpath(input.authorization)
      const bytes = await readFile(filename)
      const approval = authorization.parse(JSON.parse(bytes.toString("utf8")))
      const loop = value.store.get(identity.id)
      const spec = value.spec
      if (approval.loop_id !== loop.id || approval.contract_digest !== loop.digest || approval.policy_digest !== researchDigest(spec.policy)
        || Date.parse(approval.authorized_at) > Date.now()) throw Error("Existing-goal authorization belongs to another loop, contract or policy")
      const calls = value.store.reservations(loop.id).filter(row => row.payload.kind === "model").length
      const tokens = spec.policy.input_token_cap + spec.limits.max_output_tokens
      const cost = researchModelCost(spec.policy.input_token_cap, spec.limits.max_output_tokens, spec.policy)
      if ((cost === null) !== (approval.max_cost_usd === null))
        throw Error("Model invocation authorization must explicitly match the unbounded policy")
      if (calls + 1 > approval.max_calls || loop.ledger.total.model_tokens + tokens > approval.max_tokens
        || exceedsResearchLimit(loop.ledger.total.model_cost + (cost ?? 0), approval.max_cost_usd))
        throw Error("Next call exceeds the existing user goal's authorized cumulative budget")
      // Rehash account/pricing evidence before obtaining any provider connection.
      await Promise.all([readBoundFile(spec.policy_verification), ...spec.policy_evidence.map(readBoundFile)])
      input.abort.throwIfAborted()
      value.store.record(loop.id, `runner-authorization:${sha(bytes)}`, { schema: "research-runner-existing-goal-authorization-v1",
        path: filename, sha256: sha(bytes), authorization: approval, native_compute_authorized: false })
      const { Provider } = await import("../../src/provider/provider")
      const { Agent } = await import("../../src/agent/agent")
      const { SystemPrompt } = await import("../../src/session/system")
      const model = await Provider.getModel(spec.model.provider_id, spec.model.model_id)
      const agent = await Agent.get(spec.model.agent)
      if (!agent) throw Error("Configured research agent is unavailable")
      const result = await runResearchLiveDecision({ ...identity, policy: spec.policy, request: {
        user, sessionID: value.manifest.session_id, model, agent, abort: input.abort,
        system: await SystemPrompt.environment(model, value.manifest.session_id), limits: spec.limits,
      } })
      const evidence = report(value)
      await atomic(path.join(value.manifest.run_directory, "latest-decision-receipt.json"), { result, evidence })
      return { result, evidence }
    })
  } finally { value.store.close() }
}

/** bun run integrations/discovery-platform/research-runner.ts prepare --run ... --spec ...
 * read/recover need only --run; invoke additionally requires --authorization.
 * There is no default command and no automatic decision loop. */
export async function researchRunnerMain(args: string[]) {
  const [command, ...rest] = args
  if (!command || !["prepare", "read", "recover", "invoke"].includes(command))
    throw Error("Choose prepare, read, recover or explicit invoke; no model call is the default")
  const parsed = parseArgs({ args: rest, strict: true, allowPositionals: false,
    options: { run: { type: "string" }, spec: { type: "string" }, key: { type: "string" }, authorization: { type: "string" } } }).values
  if (!parsed.run) throw Error("--run is required")
  if (command === "prepare") {
    if (!parsed.spec || parsed.key || parsed.authorization) throw Error("prepare requires --spec and does not accept invocation flags")
    return prepareResearchRunner({ run: parsed.run, spec: researchRunnerSpec.parse(JSON.parse(await readFile(parsed.spec, "utf8"))) })
  }
  if (parsed.spec || (command !== "invoke" && parsed.authorization)) throw Error("Unexpected preparation/invocation flags")
  if (command === "read") return readResearchRunner({ run: parsed.run })
  if (command === "recover") return recoverResearchRunner({ run: parsed.run, key: parsed.key })
  if (!parsed.authorization) throw Error("invoke requires the existing user goal's --authorization record")
  return invokeResearchRunner({ run: parsed.run, key: parsed.key, authorization: parsed.authorization, abort: new AbortController().signal })
}

if (import.meta.main) {
  // Session imports the platform provider catalog transitively. Keep this
  // standalone host process on its configured/cache-pinned catalog; even a
  // local prepare/recover must not trigger the catalog's background HTTP poll.
  process.env.OPENSCIENCE_DISABLE_MODELS_FETCH = "true"
  researchRunnerMain(process.argv.slice(2)).then(result => {
    process.stdout.write(JSON.stringify(result, null, 2) + "\n")
  }).catch(error => {
    process.stderr.write((error instanceof Error ? error.message : String(error)) + "\n")
    process.exitCode = 1
  })
}
