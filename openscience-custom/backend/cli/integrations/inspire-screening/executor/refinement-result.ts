import z from "zod"
import { digest } from "./model"
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { refinementAssessment } from "./refinement-quality-policy"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const relative = z.string().min(1).refine((value) => !/[\\\x00-\x1f\x7f]/.test(value) &&
  value.split("/").every((part) => part && part !== "." && part !== ".."), "Artifact path must be contained")
const candidate = z.object({ input_sdf_index: z.number().int().nonnegative(), compound_id: z.string().min(1),
  status: z.enum(["SUCCESS", "FAILED"]), score: z.number().finite().nullable(),
  structure: relative.nullable(), structure_index: z.number().int().nonnegative().nullable(),
  failure_reason: z.string().min(1).nullable() }).strict()
const legacy = z.object({
  version: z.literal("screening-refinement-outcome-v1"), task_id: z.string(), plan_digest: hash,
  engine: z.enum(["af3", "covdock"]), selection_id: hash, selection_manifest_digest: hash,
  input_sha256: hash, input_identity_sha256: hash,
  code: z.object({ release_id: hash, manifest_sha256: hash }).strict(),
  input_count: z.number().int().min(1).max(100), success_count: z.number().int().nonnegative(),
  failed_count: z.number().int().nonnegative(),
  candidates: z.array(candidate).min(1).max(100),
  score_direction: z.literal("lower_is_better"), artifacts: z.record(relative, hash),
  gates: z.object({ input_integrity: z.literal(true), output_integrity: z.literal(true),
    coverage: z.literal(true), scientific_quality: z.literal(true) }).strict(),
}).strict()
// V1 remains readable without inventing a historical per-candidate verdict.
// V2 explicitly separates engine coverage from scientific triage. All candidates
// can be rejected by a trusted quality validator while computation is complete.
const classified = legacy.extend({
  version: z.literal("screening-refinement-outcome-v2"),
  quality_policy_digest: hash.optional(),
  quality_counts: z.object({ passed: z.number().int().nonnegative(), rejected: z.number().int().nonnegative(),
    not_evaluated: z.number().int().nonnegative() }).strict(),
  candidates: z.array(candidate.extend({ quality: z.object({
    status: z.enum(["PASSED", "REJECTED", "NOT_EVALUATED"]), reason: z.string().min(1).nullable(),
    evidence: z.array(relative).max(32),
    assessment: refinementAssessment.optional(),
  }).strict() }).strict()).min(1).max(100),
}).strict()
export const refinementResult = z.discriminatedUnion("version", [legacy, classified])

// The transport MUST freshly hash actual remote files and parse engine evidence.
// This host check additionally enforces exact identities, scope and mandatory
// evidence shape; saved success flags or a generic job exit code are not enough.
export function verifyRefinementResult(value: unknown, input: RefinementPlan) {
  const plan = refinementPlan.parse(input)
  const result = refinementResult.parse(value)
  if (result.version !== (plan.result_contract ?? "screening-refinement-outcome-v1")) {
    throw new Error("Refinement result contract differs from the independently approved plan")
  }
  if (result.task_id !== plan.id || result.plan_digest !== digest(plan) || result.engine !== plan.engine.name ||
    result.selection_id !== plan.selection_id || result.selection_manifest_digest !== plan.selection_manifest_digest ||
    result.input_sha256 !== plan.input.sha256 || result.input_identity_sha256 !== plan.input.identity_sha256 ||
    digest(result.code) !== digest(plan.code)) throw new Error("Refinement result belongs to a different immutable input/engine/code plan")
  const rows = result.candidates
  const identities = rows.map((row) => [row.input_sdf_index, row.compound_id]).sort((a, b) => Number(a[0]) - Number(b[0]))
  if (new Set(rows.map((row) => row.input_sdf_index)).size !== rows.length ||
    new Set(rows.map((row) => row.compound_id)).size !== rows.length || digest(identities) !== plan.input.identity_sha256) {
    throw new Error("Refinement output lost, duplicated or substituted candidate identities")
  }
  const successes = rows.filter((row) => row.status === "SUCCESS")
  if (rows.length !== plan.input.candidates.length || result.input_count !== rows.length ||
    result.success_count !== successes.length || result.failed_count !== rows.length - successes.length ||
    !successes.length || successes.length / rows.length < plan.engine.min_coverage) throw new Error("Refinement output denominator or minimum coverage failed")
  // CovDock may put different candidates in one Maestro container. Its audited
  // zero-based entry, not the filename alone, identifies the actual structure.
  if (new Set(successes.map((row) => JSON.stringify([row.structure, row.structure_index]))).size !== successes.length) {
    throw new Error("Different refined candidates cannot share an ambiguous structure entry")
  }
  if (successes.some((row) => plan.engine.name === "af3" ? row.structure_index !== null : row.structure_index === null)) {
    throw new Error("AF3 uses a standalone structure; CovDock requires an audited zero-based entry")
  }
  if (rows.some((row) => row.status === "SUCCESS"
    ? row.score === null || row.structure === null || !result.artifacts[row.structure] || row.failure_reason !== null
    : row.score !== null || row.structure !== null || row.structure_index !== null || !row.failure_reason?.trim())) {
    throw new Error("Refinement score/structure/failure evidence is incomplete or inconsistent")
  }
  if (result.version === "screening-refinement-outcome-v2") {
    if (result.quality_policy_digest !== (plan.quality_policy ? digest(plan.quality_policy) : undefined)) {
      throw new Error("Quality result differs from the explicitly approved structural QC policy")
    }
    const strict = plan.quality_policy?.version === "refinement-structural-qc-v2-provisional"
    for (const row of result.candidates) {
      const assessment = row.quality.assessment
      if (strict !== !!assessment) throw new Error("Independent quality axes differ from the frozen policy")
      if (!assessment) continue
      const failed = row.status === "FAILED"
      const physical = failed ? "NOT_EVALUATED" : assessment.failure_reasons.length ? "FAIL"
        : assessment.missing_reasons.length ? "UNKNOWN" : "PASS"
      const eligible = !failed && physical === "PASS"
      if (assessment.physical_validity !== physical || assessment.review_eligible !== eligible ||
        assessment.evidence_complete !== (!failed && !assessment.missing_reasons.length) ||
        failed && (assessment.failure_reasons.length || assessment.missing_reasons.length) ||
        !failed && (row.quality.status === "PASSED") !== eligible ||
        !failed && digest((row.quality.reason ?? "").split("; ").filter(Boolean).sort()) !==
          digest([...assessment.missing_reasons, ...assessment.failure_reasons].sort())) {
        throw new Error("Independent quality axes contradict execution, evidence or eligibility")
      }
    }
    const count = (status: "PASSED" | "REJECTED" | "NOT_EVALUATED") => result.candidates.filter((row) => row.quality.status === status).length
    if (result.quality_counts.passed !== count("PASSED") || result.quality_counts.rejected !== count("REJECTED") ||
      result.quality_counts.not_evaluated !== count("NOT_EVALUATED") ||
      result.quality_counts.passed + result.quality_counts.rejected !== result.success_count ||
      result.quality_counts.not_evaluated !== result.failed_count) throw new Error("Refinement quality counts differ from engine coverage")
    if (result.candidates.some((row) => row.status === "FAILED"
      ? row.quality.status !== "NOT_EVALUATED" || row.quality.reason !== null || row.quality.evidence.length !== 0
      : row.quality.status === "NOT_EVALUATED" || !row.quality.evidence.length ||
        new Set(row.quality.evidence).size !== row.quality.evidence.length ||
        row.quality.evidence.some((name) => !result.artifacts[name]) ||
        (row.quality.status === "REJECTED" ? !row.quality.reason?.trim() : row.quality.reason !== null))) {
      throw new Error("Refinement quality verdict lacks witnessed evidence or a rejection reason")
    }
  }
  return result
}
