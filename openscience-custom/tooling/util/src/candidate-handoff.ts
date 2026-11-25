import { z } from "zod"
import { asset, metric, sha256 } from "./discovery-result"
import { discoveryRoute } from "./discovery-config"
import { sourceAssessment } from "./candidate-assessment"
import { candidateEvidence } from "./candidate-evidence"
import { interpretMetric, metricInterpretation } from "./metric-semantics"
import { studyTarget, type DiscoveryStudy, type StudyCandidateSource } from "./discovery-study"

const missing = z.object({ state: z.literal("missing"), reason: z.string().min(1) }).strict()
export const candidateHandoff = z.object({
  schema: z.literal("candidate-evaluation-handoff-v1"),
  provenance: z.object({ recordID: z.string(), study: z.string(), route: discoveryRoute,
    run: z.string(), sha256, mode: z.string(), candidateID: z.string() }).strict(),
  chemicalIdentity: z.object({ state: z.literal("reported"), smiles: z.string(),
    validation: z.literal("not_checked_by_handoff") }).strict(),
  sourceEvidence: candidateEvidence.nullable(),
  sourceAssessment: sourceAssessment.optional(),
  poseProvenance: z.union([missing, candidateEvidence.shape.pose.unwrap()]),
  warhead: z.object({ state: z.literal("requested_not_verified"), requested: z.string().nullable(),
    reactionAtoms: z.union([missing, candidateEvidence.shape.reactionAtoms.unwrap()]) }).strict(),
  target: z.object({ requested: studyTarget,
    numberingMap: z.union([missing, candidateEvidence.shape.targetMapping.unwrap()]) }).strict(),
  poses: z.array(z.object({ method: z.string(), asset, format: z.string(),
    pose: z.number().int().nonnegative().nullable(), identity: z.record(z.string(), z.string()),
    bindingValidation: z.literal("not_checked_by_handoff") }).strict()),
  metrics: z.array(z.object({ scope: z.enum(["candidate", "md"]), metric,
    interpretation: metricInterpretation.optional(),
    state: z.enum(["reported_value", "missing"]),
    computationProvenance: z.union([missing, candidateEvidence.shape.measurements.element]) }).strict()),
  missingEvidence: z.array(z.string()),
  decision: z.literal("not_assessed"),
}).strict().superRefine((value, context) => {
  if (value.sourceAssessment && value.sourceAssessment.candidateID !== value.provenance.candidateID)
    context.addIssue({ code: "custom", message: "Source assessment candidate identity mismatch" })
})
export type CandidateHandoff = z.infer<typeof candidateHandoff>

/** Preserve source assertions without promoting labels or configured targets to evidence.
 * This projection performs no chemistry, coordinate mapping, ranking or thresholding.
 */
export function evaluationHandoff(source: StudyCandidateSource, study: DiscoveryStudy): CandidateHandoff {
  const candidate = source.candidate
  const evidence = candidate.scientificEvidence
  if (source.study !== study.id) throw Error("Candidate handoff belongs to another study")
  const mapping = evidence?.targetMapping
  const incompleteMetrics = (["candidate", "md"] as const).flatMap(scope =>
    (scope === "candidate" ? candidate.metrics : candidate.md.metrics)
      .filter(value => value.value !== null && !evidence?.measurements.some(item =>
        item.scope === scope && item.metricID === value.id))
      .map(value => `metric_computation_provenance:${scope}:${value.id}`))
  if (mapping && (mapping.uniprot !== study.target.uniprot || mapping.taxon !== study.target.taxon
    || (study.target.site.numbering === "canonical" && mapping.canonicalResidue !== study.target.site.number)
    || (study.target.site.numbering === "structure" && (mapping.structure.number !== study.target.site.number
      || mapping.structure.chain !== study.target.site.chain))))
    throw Error("Candidate target mapping differs from study")
  return candidateHandoff.parse({
    schema: "candidate-evaluation-handoff-v1",
    provenance: { recordID: source.recordID, study: source.study, route: source.route,
      run: source.sourceRun, sha256: source.sourceSha256, mode: source.mode, candidateID: candidate.id },
    chemicalIdentity: { state: "reported", smiles: candidate.smiles, validation: "not_checked_by_handoff" },
    sourceEvidence: candidate.scientificEvidence ?? null,
    ...(candidate.sourceAssessment ? { sourceAssessment: candidate.sourceAssessment } : {}),
    poseProvenance: evidence?.pose ?? { state: "missing",
      reason: "No coordinate-file hash and producing-stage evidence supplied" },
    warhead: { state: "requested_not_verified", requested: study.routes[source.route].inputs.warhead ?? null,
      reactionAtoms: evidence?.reactionAtoms ?? { state: "missing", reason: "No atom mapping with molecule and index convention supplied by this result contract" } },
    target: { requested: study.target,
      numberingMap: mapping ?? { state: "missing", reason: "Requested numbering is not a verified canonical-to-structure residue mapping" } },
    poses: candidate.structures.map(item => ({ ...item, pose: item.pose ?? null,
      bindingValidation: "not_checked_by_handoff" })),
    metrics: (["candidate", "md"] as const).flatMap(scope =>
      (scope === "candidate" ? candidate.metrics : candidate.md.metrics).map(value => ({
        scope, metric: value, state: value.value === null ? "missing" : "reported_value",
        interpretation: interpretMetric(value,
          evidence?.measurements.find(item => item.scope === scope && item.metricID === value.id)?.stage),
        computationProvenance: evidence?.measurements.find(item => item.scope === scope && item.metricID === value.id) ?? { state: "missing",
          reason: "Source value retained; per-metric calculation method, input hashes and execution receipt not supplied" },
      }))),
    missingEvidence: ["verified_chemical_identity", "warhead_match",
      ...(!evidence?.reactionAtoms ? ["reaction_atom_mapping"] : ["reaction_atom_mapping_verification"]),
      ...(!mapping ? ["target_numbering_mapping"] : ["target_numbering_mapping_verification"]),
      ...(!evidence?.measurements.length || incompleteMetrics.length ? ["metric_computation_provenance"] : []),
      ...incompleteMetrics,
      ...(evidence?.measurements.length ? ["metric_provenance_verification"] : []),
      ...(candidate.structures.length || evidence?.pose ? ["pose_target_binding"] : ["pose"]),
      ...(!candidate.metrics.length && !candidate.md.metrics.length ? ["metrics"] : [])],
    decision: "not_assessed",
  })
}
