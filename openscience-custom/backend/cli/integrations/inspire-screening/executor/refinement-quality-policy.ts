import z from "zod"

// A bounded structural QC policy, not an affinity classifier or a validated hit
// model. Values follow the existing CovDock geometry/strain gate. Changing them
// requires a new policy version and explicit approval, never a post-hoc cutoff.
const legacy = z.object({
  version: z.literal("refinement-structural-qc-v1"),
  scope: z.literal("bounded_structural_qc_not_affinity"),
  min_covalent_distance_angstrom: z.literal(1.5),
  max_covalent_distance_angstrom: z.literal(2.2),
  min_nonbonded_distance_angstrom: z.literal(1.2),
  covdock_max_strain_kcal_mol: z.literal(20),
  af3_confidence: z.literal("report_only_no_affinity_cutoff"),
  missing_measurements: z.literal("reject_candidate"),
  limitation: z.literal("full_topology_stereochemistry_and_affinity_not_verified"),
}).strict()
const strict = legacy.extend({
  version: z.literal("refinement-structural-qc-v2-provisional"),
  strain_source_field: z.literal("r_strain_Local_Strain_Energy"),
  calibration_status: z.literal("provisional"),
  missing_measurements: z.literal("withhold_eligibility_not_physical_failure"),
}).strict()
export const refinementQualityPolicy = z.discriminatedUnion("version", [legacy, strict])
// Historical default retained for frozen v1 plans and proposal replay.
export const defaultRefinementQuality = Object.freeze(legacy.parse({
  version: "refinement-structural-qc-v1", scope: "bounded_structural_qc_not_affinity",
  min_covalent_distance_angstrom: 1.5, max_covalent_distance_angstrom: 2.2,
  min_nonbonded_distance_angstrom: 1.2, covdock_max_strain_kcal_mol: 20,
  af3_confidence: "report_only_no_affinity_cutoff", missing_measurements: "reject_candidate",
  limitation: "full_topology_stereochemistry_and_affinity_not_verified",
}))
// Selected by new v5/v6 proposals; existing frozen approvals keep their own policy.
export const provisionalRefinementQuality = Object.freeze(strict.parse({ ...defaultRefinementQuality,
  version: "refinement-structural-qc-v2-provisional", strain_source_field: "r_strain_Local_Strain_Energy",
  calibration_status: "provisional", missing_measurements: "withhold_eligibility_not_physical_failure",
}))

export const refinementAssessment = z.object({
  version: z.literal("refinement-independent-axes-v1"),
  physical_validity: z.enum(["PASS", "FAIL", "UNKNOWN", "NOT_EVALUATED"]),
  evidence_complete: z.boolean(), review_eligible: z.boolean(),
  missing_reasons: z.array(z.string().min(1)), failure_reasons: z.array(z.string().min(1)),
  calibration_status: z.literal("provisional"),
}).strict()

const number = z.number().finite().nullable()
export const refinementQualityMeasurements = z.object({
  version: z.literal("refinement-quality-measurements-v1"), policy_digest: z.string().regex(/^[a-f0-9]{64}$/),
  candidates: z.array(z.object({ input_sdf_index: z.number().int().nonnegative(), compound_id: z.string().min(1),
    evidence: z.array(z.string().min(1)).max(12),
    metrics: z.discriminatedUnion("engine", [
      z.object({ engine: z.literal("af3"), mpae: z.number().finite().nonnegative(),
        iptm: z.number().finite().min(0).max(1), ligand_plddt_mean: z.number().finite().min(0).max(100),
        has_clash: z.boolean() }).strict(),
      z.object({ engine: z.literal("covdock"), covalent_link_present: z.boolean().nullable(),
        geometry_inspection_status: z.string().nullable(), target_atom_match_count: number,
        external_target_bond_count: number, ligand_heavy_atom_count: number,
        covalent_bond_length_angstrom: number, minimum_nonbonded_heavy_atom_distance_angstrom: number,
        severe_clash: z.boolean().nullable(), strain_kcal_mol: number,
        strain_provenance: z.object({ source_field: z.literal("r_strain_Local_Strain_Energy"),
          unit: z.literal("kcal/mol"), definition: z.literal("vendor_local_strain_unmodified"),
          status: z.enum(["VALID", "MISSING", "INVALID_OR_NONFINITE"]),
        }).strict().optional() }).strict(),
    ]).nullable(),
  }).strict()).min(1).max(100),
}).strict()
