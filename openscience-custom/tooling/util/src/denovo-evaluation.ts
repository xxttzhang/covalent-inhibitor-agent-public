import type { StudyCandidateSource, DiscoveryStudy } from "./discovery-study"
import { evaluationHandoff } from "./candidate-handoff"
import { warheadDefinition } from "./warhead-registry"

/** A required-evidence checklist, not a substitute for scientific evaluators.
 * Reported source measurements never silently become gate passes. */
export function deNovoEvaluation(source: StudyCandidateSource, study: DiscoveryStudy) {
  const handoff = source.handoff ?? evaluationHandoff(source, study)
  if (handoff.provenance.study !== study.id || handoff.provenance.recordID !== source.recordID)
    throw Error("De novo evaluation identity mismatch")
  return { schema: "denovo-evaluation-requirements-v1", candidate: source.candidate.id,
    study: study.id, origin: study.denovoOrigin ?? null, run: source.sourceRun,
    warhead: warheadDefinition(study.routes["de-novo"].inputs.warhead ?? ""),
    handoff, sourceAssessment: handoff.sourceAssessment ?? null,
    stages: [
      { stage: "chemical_identity", state: "requires_verification", required: ["Molecular validity", "Exactly one designated reactive warhead", "Reaction atom identity", "Synthesis feasibility"] },
      { stage: "pre_reaction_pose", state: "not_independently_assessed", required: ["Bound receptor and residue numbering", "Noncovalent recognition", "Mechanism-specific attack geometry", "Clashes and strain"] },
      { stage: "adduct", state: "not_independently_assessed", required: ["Reaction-specific atom/bond/proton/charge mapping", "Stereochemistry and tautomer assignment", "Product pose refinement"] },
      { stage: "md_admission", state: "not_admitted", required: ["Verified selected pose", "Covalent topology and parameter validation", "System preparation and short QC", "Separate frozen plan and native approval"] },
      { stage: "experimental_validation", state: "not_established", required: ["Target engagement", "Reaction kinetics and reversibility", "Selectivity", "Activity"] },
    ],
    decision: "not_assessed", numerical_thresholds_invented: false,
    note: "Inspect original structural QC and failures in the source workflow. This checklist does not overturn failure or grant pose/MD admission." }
}
