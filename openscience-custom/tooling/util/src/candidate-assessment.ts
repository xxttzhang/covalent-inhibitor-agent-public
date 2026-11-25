import { z } from "zod"

/** Producer assertions retained through route merging. This does not rerun QC
 * or turn a workflow failure into molecular or biological invalidity. */
export const sourceAssessment = z.object({
  schema: z.literal("candidate-source-assessment-v1"),
  scope: z.literal("producer_structural_qc"),
  candidateID: z.string().min(1),
  status: z.enum(["PASS", "REJECTED", "UNKNOWN", "NOT_EVALUATED"]),
  producerVerdict: z.enum(["PASS", "FAIL"]),
  evaluationState: z.enum(["COMPLETED", "ERROR", "UNAVAILABLE", "UNKNOWN"]),
  sourceAvailability: z.enum(["IDENTIFIED_MOLECULE", "UNPARSED_SOURCE", "NO_SOURCE_REPORTED"]),
  failures: z.array(z.string()),
  missingReasons: z.array(z.string()),
  evidence: z.object({ path: z.string().min(1), sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict(),
  verification: z.literal("receipt_consistency_only"),
  physicalValidity: z.literal("not_assessed_by_reader"),
  independentEvaluation: z.literal("NOT_PERFORMED"),
}).strict().superRefine((value, context) => {
  const expected = value.producerVerdict === "PASS" ? "PASS"
    : value.evaluationState === "ERROR" ? "NOT_EVALUATED"
    : value.evaluationState === "COMPLETED" ? "REJECTED" : "UNKNOWN"
  if (value.status !== expected || value.producerVerdict === "PASS" &&
    (value.evaluationState !== "COMPLETED" || value.sourceAvailability !== "IDENTIFIED_MOLECULE"
      || value.failures.length || value.missingReasons.length))
    context.addIssue({ code: "custom", message: "Source assessment contradicts producer evidence" })
})
export type SourceAssessment = z.infer<typeof sourceAssessment>

export function deNovoSourceAssessment(row: {
  candidate_id: string; structural_qc_passed: boolean; smiles: string | null; source_sha256: string | null; failures: string[];
}, evidence: { path: string; sha256: string }): SourceAssessment {
  const error = row.failures.includes("evaluation_error")
  const identified = !!row.smiles && !!row.source_sha256
  const evaluationState = error ? "ERROR" : identified ? "COMPLETED"
    : row.source_sha256 ? "UNKNOWN" : "UNAVAILABLE"
  return sourceAssessment.parse({
    schema: "candidate-source-assessment-v1", scope: "producer_structural_qc", candidateID: row.candidate_id,
    status: row.structural_qc_passed ? "PASS" : error ? "NOT_EVALUATED" : identified ? "REJECTED" : "UNKNOWN",
    producerVerdict: row.structural_qc_passed ? "PASS" : "FAIL", evaluationState,
    sourceAvailability: identified ? "IDENTIFIED_MOLECULE" : row.source_sha256 ? "UNPARSED_SOURCE" : "NO_SOURCE_REPORTED",
    failures: row.failures,
    missingReasons: [...(!row.source_sha256 ? ["source_file_hash_not_reported"] : []),
      ...(!row.smiles ? ["chemical_identity_not_resolved"] : []), ...(error ? ["structural_evaluation_error"] : [])],
    evidence, verification: "receipt_consistency_only", physicalValidity: "not_assessed_by_reader",
    independentEvaluation: "NOT_PERFORMED",
  })
}

export function assessmentLabel(value: SourceAssessment) {
  if (value.status === "PASS") return "结构 QC 通过，待独立评价"
  if (value.status === "REJECTED") return "结构 QC 未通过（原规则）"
  if (value.status === "NOT_EVALUATED") return "结构评价未完成"
  return "结构证据不足"
}
