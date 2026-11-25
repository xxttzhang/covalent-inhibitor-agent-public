import { digest, type Task } from "./model"
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { verifyReadback, verifyReadbackOutcome } from "./refinement-readback"
import { ReviewRequired } from "./runner"

// Fixed production decision path, not a model-provided verdict. PASSED means
// only that this approved bounded structural QC passed. Confidence and docking
// scores remain descriptive; complete topology/stereochemistry/affinity are not
// verified. Zero passing candidates is a legitimate fully evaluated outcome.
export function evaluateRefinementQuality(input: RefinementPlan, parent: Task, value: unknown) {
  const plan = refinementPlan.parse(input)
  const policy = plan.quality_policy
  if (!policy) throw new ReviewRequired("No explicitly approved structural quality policy")
  const strict = policy.version === "refinement-structural-qc-v2-provisional"
  const assessment = (reasons: string[], failed = false) => {
    if (!strict) return {}
    const missing = reasons.filter((reason) => reason.endsWith("_missing") || reason.endsWith("_not_verified"))
    const failures = reasons.filter((reason) => !missing.includes(reason))
    return { assessment: { version: "refinement-independent-axes-v1" as const,
      physical_validity: failed ? "NOT_EVALUATED" as const : failures.length ? "FAIL" as const
        : missing.length ? "UNKNOWN" as const : "PASS" as const,
      evidence_complete: !failed && !missing.length, review_eligible: !failed && !reasons.length,
      missing_reasons: missing, failure_reasons: failures, calibration_status: "provisional" as const } }
  }
  const files = verifyReadback(value, plan, parent)
  if (!files.quality) throw new ReviewRequired("Missing independently read quality measurements")
  const candidates = files.evidence.candidates.map((source) => {
    const row = { input_sdf_index: source.input_sdf_index, compound_id: source.compound_id, status: source.engine_status,
      score: source.score, structure: source.structure?.path ?? null, structure_index: source.structure?.pose_index ?? null,
      failure_reason: source.failure_reason }
    if (row.status === "FAILED") return { ...row, quality: { status: "NOT_EVALUATED" as const, reason: null, evidence: [], ...assessment([], true) } }
    const measured = files.quality!.candidates.find((item) => item.input_sdf_index === row.input_sdf_index)!
    const metrics = measured.metrics!
    const reasons: string[] = []
    const distance = (value: number | null) => {
      if (value === null) reasons.push("covalent_distance_missing")
      else if (value < policy.min_covalent_distance_angstrom || value > policy.max_covalent_distance_angstrom) reasons.push("covalent_distance_outside_approved_range")
    }
    const contact = (value: number | null) => {
      if (value === null) reasons.push("nonbonded_contact_measurement_missing")
      else if (value < policy.min_nonbonded_distance_angstrom) reasons.push("severe_nonbonded_contact")
    }
    if (metrics.engine === "af3") {
      const geometry = files.geometry?.candidates.find((item) => item.input_sdf_index === row.input_sdf_index)?.measurements
      if (!geometry) throw new ReviewRequired("AF3 QC lacks original geometry measurements")
      if (!geometry.model_has_exact_covalent_link || geometry.model_target_covalent_link_count !== 1) reasons.push("exact_target_covalent_link_missing_or_ambiguous")
      if (metrics.has_clash) reasons.push("af3_reported_clash")
      distance(geometry.reactive_distance_angstrom)
      contact(geometry.minimum_nonbonded_heavy_atom_distance_angstrom)
    } else {
      if (metrics.geometry_inspection_status !== "SUCCESS") reasons.push("covdock_geometry_not_verified")
      // Failed inspection does not authenticate its geometry. Legacy plans keep
      // their original rejection reasons; the new policy separates missing data.
      if (!strict || metrics.geometry_inspection_status === "SUCCESS") {
        if (strict) {
          if (metrics.covalent_link_present === null || metrics.target_atom_match_count === null || metrics.external_target_bond_count === null) reasons.push("target_link_measurement_missing")
          if (metrics.covalent_link_present === false || metrics.target_atom_match_count !== null && metrics.target_atom_match_count !== 1 ||
            metrics.external_target_bond_count !== null && metrics.external_target_bond_count !== 1) reasons.push("exact_target_covalent_link_missing_or_ambiguous")
        } else if (metrics.covalent_link_present !== true || metrics.target_atom_match_count !== 1 || metrics.external_target_bond_count !== 1) reasons.push("exact_target_covalent_link_missing_or_ambiguous")
        if (metrics.ligand_heavy_atom_count === null || !Number.isInteger(metrics.ligand_heavy_atom_count) || metrics.ligand_heavy_atom_count <= 0) {
          reasons.push(strict && metrics.ligand_heavy_atom_count !== null ? "ligand_component_invalid" : "ligand_component_not_verified")
        }
        distance(metrics.covalent_bond_length_angstrom)
        contact(metrics.minimum_nonbonded_heavy_atom_distance_angstrom)
        if (metrics.severe_clash === null) reasons.push("clash_check_missing")
        else if (metrics.severe_clash) reasons.push("covdock_reported_clash")
      }
      if (metrics.strain_kcal_mol === null) reasons.push("ligand_strain_missing")
      else if (metrics.strain_kcal_mol > policy.covdock_max_strain_kcal_mol) reasons.push("ligand_strain_above_approved_gate")
    }
    return { ...row, quality: { status: reasons.length ? "REJECTED" as const : "PASSED" as const,
      reason: reasons.length ? reasons.join("; ") : null, evidence: measured.evidence, ...assessment(reasons) } }
  })
  const success = candidates.filter((row) => row.status === "SUCCESS").length
  const result = verifyReadbackOutcome({ version: "screening-refinement-outcome-v2", task_id: plan.id, plan_digest: digest(plan),
    engine: plan.engine.name, selection_id: plan.selection_id, selection_manifest_digest: plan.selection_manifest_digest,
    input_sha256: plan.input.sha256, input_identity_sha256: plan.input.identity_sha256, code: plan.code,
    input_count: candidates.length, success_count: success, failed_count: candidates.length - success, candidates,
    quality_policy_digest: digest(policy),
    quality_counts: { passed: candidates.filter((row) => row.quality.status === "PASSED").length,
      rejected: candidates.filter((row) => row.quality.status === "REJECTED").length,
      not_evaluated: candidates.filter((row) => row.quality.status === "NOT_EVALUATED").length },
    score_direction: files.evidence.score_direction, artifacts: files.evidence.artifacts,
    // This gate means the bounded QC was applied completely, not that all passed.
    gates: { input_integrity: true, output_integrity: true, coverage: true, scientific_quality: true },
  }, plan, files)
  if (result.version !== "screening-refinement-outcome-v2") throw new ReviewRequired("Structural QC requires classified results")
  return result
}
