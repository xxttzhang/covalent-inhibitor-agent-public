import z from "zod"
import { digest, type Task } from "./model"
import { refinementCommand } from "./refinement-command"
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { RefinementPreparation, preparationArgs } from "./refinement-preparation"
import type { RefinementRun } from "./refinement-runs"
import { ReviewRequired } from "./runner"
import { verifyRefinementResult } from "./refinement-result"
import { refinementPaths } from "./refinement-runtime"
import { refinementQualityMeasurements } from "./refinement-quality-policy"
import { CLIError } from "./inspire"
import { readbackRevision } from "./readback-revision"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const relative = z.string().min(1).max(2048).refine((value) => !/[\\\x00-\x1f\x7f]/.test(value) &&
  value.split("/").every((part) => part && part !== "." && part !== ".."))
const geometry = z.object({ version: z.literal("af3-geometry-evidence-v1"), input_sha256: hash, target_sha256: hash,
  scientific_quality_verified: z.literal(false), candidates: z.array(z.object({
    input_sdf_index: z.number().int().nonnegative(), compound_id: z.string().min(1), state: z.enum(["MEASURED", "NOT_EVALUATED"]),
    measurements: z.object({ source_residue: z.literal(797), af3_sequence_residue: z.number().int().positive(),
      nucleophilic_atom: z.literal("SG"), electrophilic_atom: z.string().min(1), reactive_distance_angstrom: z.number().finite().positive(),
      model_target_covalent_link_count: z.number().int().nonnegative(), model_has_exact_covalent_link: z.boolean(),
      ligand_heavy_atom_count: z.number().int().positive(), protein_heavy_atom_count: z.number().int().positive(),
      minimum_nonbonded_heavy_atom_distance_angstrom: z.number().finite().nonnegative().nullable(), nonbonded_pair_count: z.number().int().nonnegative(),
      distance_scope: z.literal("protein_ligand_heavy_atoms_excluding_declared_reactive_pair"),
      full_topology_and_stereochemistry_verified: z.literal(false), scientific_quality_verified: z.literal(false) }).strict().nullable(),
  }).strict()).min(1).max(100) }).strict()
const evidence = z.object({ version: z.literal("refinement-artifact-evidence-v1"), engine: z.enum(["af3", "covdock"]),
  input_contract: z.object({ input_count: z.number().int().min(1).max(100), input_sha256: hash,
    identity_sha256: hash, verified: z.literal(true), grants_resource_approval: z.literal(false) }).strict(),
  candidates: z.array(z.object({ input_sdf_index: z.number().int().nonnegative(), compound_id: z.string().min(1),
    engine_status: z.enum(["SUCCESS", "FAILED"]), score: z.number().finite().nullable(),
    structure: z.object({ path: relative, pose_index: z.number().int().nonnegative().nullable() }).strict().nullable(),
    failure_reason: z.string().min(1).nullable() }).strict()).min(1).max(100),
  artifacts: z.record(relative, hash).refine((items) => Object.keys(items).length > 0 && Object.keys(items).length <= 800),
  score_direction: z.literal("lower_is_better"), scientific_quality_verified: z.literal(false),
  resource_approval_granted: z.literal(false) }).strict()
const receipt = z.object({ version: z.literal("refinement-readback-v1"), id: z.string(), digest: hash,
  code: z.object({ release_id: hash, manifest_sha256: hash }).strict(), selection_id: hash,
  selection_manifest_digest: hash, output: z.string(), manifest_sha256: hash, evidence, geometry: geometry.optional(),
  quality: refinementQualityMeasurements.optional(),
  reader_revision: z.object({ version: z.literal("covdock-cached-coordinates-v1"), plan_digest: hash,
    original_sha256: hash, path: z.string(), sha256: hash }).strict().optional() }).strict()
export type RefinementReadback = z.infer<typeof receipt>

// File/identity evidence only. Never sets scientific_quality=true or completes a run.
export function verifyReadback(value: unknown, input: RefinementPlan, parent: Task) {
  const plan = refinementPlan.parse(input)
  const result = receipt.parse(value)
  if (digest(result.reader_revision ?? null) !== digest(readbackRevision(plan, parent.plan.runtime.root))) {
    throw new ReviewRequired("Readback revision differs from the registered repair")
  }
  const current = result.evidence
  const contract = current.input_contract
  if (result.id !== plan.id || result.digest !== digest(plan) || digest(result.code) !== digest(plan.code) ||
      result.selection_id !== plan.selection_id || result.selection_manifest_digest !== plan.selection_manifest_digest ||
      result.output !== refinementCommand(plan, parent).output || current.engine !== plan.engine.name ||
      contract.input_count !== plan.input.candidates.length || contract.input_sha256 !== plan.input.sha256 ||
      contract.identity_sha256 !== plan.input.identity_sha256) throw new ReviewRequired("Refinement artifacts differ from the approved plan")
  const rows = current.candidates
  const identities = rows.map((row) => [row.input_sdf_index, row.compound_id]).sort((a, b) => Number(a[0]) - Number(b[0]))
  if (rows.length !== contract.input_count || new Set(rows.map((row) => row.input_sdf_index)).size !== rows.length ||
      new Set(rows.map((row) => row.compound_id)).size !== rows.length || digest(identities) !== contract.identity_sha256) {
    throw new ReviewRequired("Artifact readback lost, duplicated or substituted approved candidate identities")
  }
  const successes = rows.filter((row) => row.engine_status === "SUCCESS")
  if (new Set(successes.map((row) => JSON.stringify(row.structure))).size !== successes.length || rows.some((row) =>
    row.engine_status === "FAILED" ? row.score !== null || row.structure !== null || !row.failure_reason?.trim() :
    row.score === null || !row.structure || row.failure_reason !== null || !current.artifacts[row.structure.path] ||
    (plan.engine.name === "af3" ? row.score < 0 || row.structure.pose_index !== null : row.structure.pose_index === null))) {
    throw new ReviewRequired("Artifact readback has inconsistent score, structure or failure evidence")
  }
  if (!!plan.geometry_contract !== !!result.geometry) throw new ReviewRequired("Readback geometry contract differs from approval")
  if (result.geometry) {
    const value = result.geometry
    const target = refinementPaths(plan.runtime, parent.plan.runtime.root, plan.code.release_id).target
    if (plan.runtime.name !== "af3" || value.input_sha256 !== plan.input.sha256 || value.target_sha256 !== plan.assets[target] ||
      value.candidates.length !== rows.length || new Set(value.candidates.map((row) => row.input_sdf_index)).size !== rows.length) {
      throw new ReviewRequired("Geometry input/target identities differ from approval")
    }
    const byID = new Map(rows.map((row) => [row.input_sdf_index, row]))
    for (const row of value.candidates) {
      const source = byID.get(row.input_sdf_index)
      const metrics = row.measurements
      if (!source || source.compound_id !== row.compound_id ||
        (source.engine_status === "FAILED" ? row.state !== "NOT_EVALUATED" || metrics !== null : row.state !== "MEASURED" || metrics === null)) {
        throw new ReviewRequired("Geometry must preserve each engine outcome")
      }
      if (metrics && (metrics.af3_sequence_residue !== plan.runtime.cysteine_sequence_index ||
        (metrics.model_has_exact_covalent_link && metrics.model_target_covalent_link_count !== 1) ||
        metrics.nonbonded_pair_count !== metrics.ligand_heavy_atom_count * metrics.protein_heavy_atom_count - 1 ||
        (metrics.minimum_nonbonded_heavy_atom_distance_angstrom === null) !== (metrics.nonbonded_pair_count === 0))) {
        throw new ReviewRequired("Geometry counts/target/measurements contradict their provenance")
      }
    }
  }
  if (!!plan.quality_policy !== !!result.quality) throw new ReviewRequired("Readback quality policy differs from approval")
  if (result.quality) {
    const quality = result.quality
    if (quality.policy_digest !== digest(plan.quality_policy) || quality.candidates.length !== rows.length ||
      new Set(quality.candidates.map((row) => row.input_sdf_index)).size !== rows.length) throw new ReviewRequired("Quality policy or denominator changed")
    for (const row of quality.candidates) {
      const source = rows.find((candidate) => candidate.input_sdf_index === row.input_sdf_index)
      if (!source || source.compound_id !== row.compound_id || (source.engine_status === "FAILED"
        ? row.metrics !== null || row.evidence.length !== 0
        : !row.metrics || row.metrics.engine !== plan.engine.name || !row.evidence.length ||
          new Set(row.evidence).size !== row.evidence.length || row.evidence.some((name) => !current.artifacts[name]) ||
          !row.evidence.includes(source.structure!.path))) throw new ReviewRequired("Quality measurements lost candidate or artifact provenance")
      if (row.metrics?.engine === "af3" && row.metrics.mpae !== source.score) throw new ReviewRequired("AF3 quality mPAE differs from witnessed score")
      if (row.metrics?.engine === "covdock") {
        const provenance = row.metrics.strain_provenance
        const strict = plan.quality_policy?.version === "refinement-structural-qc-v2-provisional"
        if (strict !== !!provenance || provenance &&
          (provenance.status === "VALID") !== (row.metrics.strain_kcal_mol !== null)) {
          throw new ReviewRequired("Local strain provenance differs from the frozen quality policy or value")
        }
      }
    }
  }
  return result
}

export function verifyReadbackOutcome(value: unknown, plan: RefinementPlan, files: RefinementReadback) {
  const result = verifyRefinementResult(value, plan)
  if (Object.entries(files.evidence.artifacts).some(([name, checksum]) => result.artifacts[name] !== checksum)) {
    throw new ReviewRequired("Scientific outcome dropped or changed freshly verified artifacts")
  }
  if (result.version === "screening-refinement-outcome-v2" && digest(result.artifacts) !== digest(files.evidence.artifacts)) {
    throw new ReviewRequired("Classified outcome contains artifacts outside the freshly verified inventory")
  }
  const rows = new Map(files.evidence.candidates.map((row) => [row.input_sdf_index, row]))
  if (result.candidates.some((row) => {
    const source = rows.get(row.input_sdf_index)
    if (!source || row.compound_id !== source.compound_id) return true
    // A scientific rejection retains its successful engine row and its actual
    // score/structure. It cannot be relabelled as an engine failure (or vice versa).
    if (result.version === "screening-refinement-outcome-v2" && row.status !== source.engine_status) return true
    if (row.status === "FAILED") return source.engine_status === "FAILED" && row.failure_reason !== source.failure_reason
    return source.engine_status !== "SUCCESS" || row.score !== source.score || row.structure !== source.structure?.path ||
      row.structure_index !== source.structure?.pose_index
  })) throw new ReviewRequired("Scientific outcome substituted the verified candidate score, structure or failure")
  return result
}

export class RefinementReadbacks {
  constructor(readonly preparation: RefinementPreparation) {
    preparation.runs.approvals.store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_readback(
      id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, receipt TEXT NOT NULL, observed INTEGER NOT NULL)`)
  }
  bound(plan: RefinementPlan, input: RefinementRun) {
    const parent = this.preparation.bound(plan, input)
    const run = this.preparation.runs.owned(plan.id, input.lease_owner!)
    if (!["VERIFYING", "SUCCEEDED"].includes(run.state) || run.submitted === null || run.handle !== run.name) {
      throw new ReviewRequired("Output reads require a reconciled completed job, not an unsubmitted or cancelled run")
    }
    return parent
  }
  saved(plan: RefinementPlan) {
    const row = this.preparation.runs.approvals.store.db.query("SELECT plan_digest,receipt FROM refinement_readback WHERE id=?").get(plan.id) as
      { plan_digest: string; receipt: string } | null
    if (row && row.plan_digest !== digest(plan)) throw new ReviewRequired("Retained result witness belongs to another plan")
    return row ? receipt.parse(JSON.parse(row.receipt)) : null
  }
  async invoke(plan: RefinementPlan, input: RefinementRun, witness: string | null) {
    const parent = this.bound(plan, input)
    const args = await preparationArgs(plan, parent, "outputs", witness)
    this.bound(plan, input)
    const response = z.object({ returncode: z.number(), output: z.string().max(512 * 1024) }).parse(await this.preparation.cli.call(args).catch((error) => {
      if (error instanceof CLIError && ["CommandFailed", "remote_worker_failed"].includes(error.code)) {
        throw new ReviewRequired("Remote refinement output verification failed; scheduler success is not scientific completion. Inspect retained engine evidence; do not resubmit.")
      }
      throw error // Network interruption is not evidence of an invalid scientific result.
    }))
    this.bound(plan, input)
    if (response.returncode !== 0) throw new ReviewRequired("Remote artifact verifier rejected the completed output; inspect retained evidence")
    const result = verifyReadback(JSON.parse(response.output), plan, parent)
    if (witness !== null && result.manifest_sha256 !== witness) throw new ReviewRequired("Remote result witness changed")
    return result
  }
  async read(plan: RefinementPlan, input: RefinementRun) {
    this.bound(plan, input)
    await this.preparation.read(plan, input)
    const saved = this.saved(plan)
    const initial = saved ?? await this.invoke(plan, input, null)
    // Persist the first verified observation before a second network read. A
    // timeout afterwards must not permit replacing that witness on the next tick.
    this.preparation.runs.approvals.store.db.transaction(() => {
      this.bound(plan, input)
      const existing = this.saved(plan)
      if (existing && digest(existing) !== digest(initial)) throw new ReviewRequired("Result witness differs from the retained observation")
      this.preparation.runs.approvals.store.db.query("INSERT OR IGNORE INTO refinement_readback(id,plan_digest,receipt,observed) VALUES(?,?,?,?)")
        .run(plan.id, digest(plan), JSON.stringify(initial), Date.now())
    })()
    const fresh = await this.invoke(plan, input, initial.manifest_sha256)
    if (digest(fresh) !== digest(initial)) throw new ReviewRequired("Refinement evidence changed since the first verified observation")
    await this.preparation.read(plan, input)
    this.bound(plan, input)
    return fresh
  }
}
