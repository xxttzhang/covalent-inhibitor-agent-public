import { createHash } from "node:crypto"
import { readFile, realpath } from "node:fs/promises"
import path from "node:path"
import { z } from "zod"

const text = z.string().min(1)
const hash = z.string().regex(/^[a-f0-9]{64}$/)
const count = z.number().int().nonnegative()
const status = z.enum(["pass", "fail", "unknown"])
const digest = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex")
const unique = (values: string[]) => [...new Set(values)]

/** Stable across key ordering and process restarts; array order remains meaningful. */
export function researchEvidenceHash(value: unknown): string {
  const canonical = (input: unknown): unknown => {
    if (Array.isArray(input)) return input.map(canonical)
    if (input !== null && typeof input === "object") return Object.fromEntries(Object.entries(input).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]))
    return input
  }
  return digest(JSON.stringify(canonical(value)))
}

export const ResearchArtifactSchema = z.object({
  ref: text,
  path: text,
  sha256: hash.nullable(),
  expected_sha256: hash.nullable(),
  size_bytes: count.nullable(),
  status: z.enum(["verified", "captured", "missing", "mismatch"]),
}).strict()

export const ResearchCheckSchema = z.object({
  id: text,
  status,
  evaluator: text.nullable(),
  evidence_refs: z.array(text),
  detail: text,
}).strict()

export const ResearchObservationInputSchema = z.object({
  origin: z.enum(["historical_import", "native_execution", "replay_fixture"]),
  run: z.object({
    id: text,
    task_id: text.nullable(),
    route: z.literal("library-first"),
    release_id: hash.nullable(),
    input_identity_digest: hash.nullable(),
    started_at: z.string().datetime().nullable(),
    status: z.enum(["queued", "running", "succeeded", "failed", "cancelled", "unknown"]),
    phase: text,
  }).strict(),
  counts: z.object({
    input_records: count.nullable(),
    selected: count.nullable(),
    attempted: count.nullable(),
    execution_succeeded: count.nullable(),
    scientific_passed: count.nullable(),
  }).strict(),
  candidates: z.array(z.object({
    id: text,
    smiles: text.nullable(),
    execution_status: z.enum(["succeeded", "failed", "unknown"]),
    scientific_status: status,
    scores: z.array(z.object({ engine: text, value: z.number().finite(), direction: z.enum(["higher_is_better", "lower_is_better"]) }).strict()),
  }).strict()),
  artifacts: z.array(ResearchArtifactSchema),
  identity: z.object({ status, evidence_refs: z.array(text), detail: text }).strict(),
  execution_checks: z.array(ResearchCheckSchema),
  scientific_checks: z.array(ResearchCheckSchema),
  required_scientific_checks: z.array(text).min(1),
  // Optional so historical evidence serialization and digests stay unchanged.
  // A selector can complete with zero candidates while the native pipeline
  // truthfully remains FAILED and never enters docking.
  selection_outcome: z.object({ status: z.literal("zero_candidates"),
    evaluator: z.literal("screening-zero-candidates-v1"), evidence_refs: z.array(text).min(1) }).strict().optional(),
  failure: z.object({ category: z.enum(["input", "environment", "scheduler", "engine", "cancelled", "unknown"]), detail: text, evidence_refs: z.array(text).min(1) }).strict().nullable(),
  integrity_errors: z.array(text),
  limitations: z.array(text),
}).strict()

export type ResearchObservationInput = z.infer<typeof ResearchObservationInputSchema>
export const ResearchObservationSchema = ResearchObservationInputSchema.extend({
  schema: z.literal("drug-design-observation-v1"),
  id: z.string().regex(/^obs-[a-f0-9]{64}$/),
  classification: z.enum(["objective_met", "zero_candidates", "scientific_failure", "execution_failure", "incomplete", "integrity_failure"]),
  missing_evidence: z.array(text),
  evidence_refs: z.array(text),
  new_compute: z.boolean(),
}).strict()
export type ResearchObservation = z.infer<typeof ResearchObservationSchema>

/** Host-owned adapter boundary. Never accept this input directly from model arguments. */
export function createResearchObservation(input: ResearchObservationInput): ResearchObservation {
  const parsed = ResearchObservationInputSchema.parse(input)
  const errors = [...parsed.integrity_errors]
  const refs = new Set(parsed.artifacts.filter(item => item.status === "verified" || item.status === "captured").map(item => item.ref))
  const missing = parsed.artifacts.filter(item => item.status === "missing").map(item => `artifact:${item.path}`)
  const checkrefs = [...parsed.execution_checks, ...parsed.scientific_checks, parsed.identity, ...(parsed.failure ? [parsed.failure] : [])]
  if (parsed.selection_outcome?.evidence_refs.some(ref => !refs.has(ref))) errors.push("unresolved_selection_evidence_reference")
  for (const item of parsed.artifacts) {
    if (item.status === "verified" && (!item.sha256 || item.sha256 !== item.expected_sha256)) errors.push("invalid_verified_artifact")
    if ((item.status === "captured" || item.status === "verified") && (item.sha256 === null || item.size_bytes === null)) errors.push("missing_artifact_content_identity")
  }
  if (parsed.artifacts.some(item => item.status === "mismatch")) errors.push("artifact_hash_mismatch")
  if (unique(parsed.artifacts.map(item => item.ref)).length !== parsed.artifacts.length) errors.push("duplicate_artifact_reference")
  if (unique(parsed.candidates.map(item => item.id)).length !== parsed.candidates.length) errors.push("duplicate_candidate_identity")
  if (unique(parsed.scientific_checks.map(item => item.id)).length !== parsed.scientific_checks.length) errors.push("duplicate_scientific_check")
  if (unique(parsed.required_scientific_checks).length !== parsed.required_scientific_checks.length) errors.push("duplicate_required_scientific_check")
  for (const check of checkrefs) {
    if (check.evidence_refs.some(ref => !refs.has(ref))) errors.push("unresolved_evidence_reference")
    if ("status" in check && check.status !== "unknown" && !check.evidence_refs.length) errors.push("unsupported_check_claim")
    if ("evaluator" in check && "status" in check && check.status !== "unknown" && !check.evaluator) errors.push("missing_evaluator_identity")
  }
  if (parsed.identity.status === "fail") errors.push("candidate_identity_mismatch")
  if (parsed.identity.status === "unknown") missing.push("candidate_identity")
  if (!parsed.artifacts.length) missing.push("result_artifacts")
  if (parsed.counts.selected !== null && parsed.candidates.length !== parsed.counts.selected) errors.push("candidate_denominator_mismatch")
  const counters = [parsed.counts.input_records, parsed.counts.selected, parsed.counts.attempted, parsed.counts.execution_succeeded, parsed.counts.scientific_passed]
  for (const [index, value] of counters.entries()) {
    const previous = counters[index - 1]
    if (index > 0 && value !== null && previous !== null && previous !== undefined && value > previous) errors.push("invalid_denominator_order")
  }
  if (parsed.counts.execution_succeeded !== null && parsed.candidates.filter(item => item.execution_status === "succeeded").length !== parsed.counts.execution_succeeded) errors.push("execution_success_count_mismatch")
  if (parsed.counts.scientific_passed !== null && parsed.candidates.filter(item => item.scientific_status === "pass").length !== parsed.counts.scientific_passed) errors.push("scientific_success_count_mismatch")
  if (parsed.candidates.some(item => item.scientific_status === "pass" && item.execution_status !== "succeeded")) errors.push("scientific_success_without_execution")
  const required = parsed.required_scientific_checks.map(id => parsed.scientific_checks.find(item => item.id === id))
  parsed.required_scientific_checks.forEach((id, index) => { if (!required[index] || required[index]?.status === "unknown") missing.push(`scientific_check:${id}`) })
  if (parsed.counts.scientific_passed === null) missing.push("scientific_passed_count")
  if (parsed.candidates.some(item => item.scientific_status === "unknown")) missing.push("candidate_scientific_evaluation")
  if (parsed.run.release_id === null) missing.push("execution_release_id")
  if (parsed.run.input_identity_digest === null) missing.push("input_identity_digest")
  if (parsed.counts.selected === null) missing.push("selected_candidate_count")
  if (parsed.counts.attempted === null) missing.push("attempted_candidate_count")
  const classification = (() => {
    if (errors.length) return "integrity_failure" as const
    if (parsed.selection_outcome && parsed.origin === "native_execution" && parsed.run.status === "failed" && parsed.run.phase === "prepare"
      && parsed.run.task_id !== null && parsed.run.release_id !== null && parsed.run.input_identity_digest !== null
      && parsed.failure === null && parsed.execution_checks.length > 0 && !parsed.execution_checks.some(item => item.status !== "pass")
      && parsed.counts.selected === 0 && parsed.counts.attempted === 0 && parsed.counts.execution_succeeded === 0
      && parsed.counts.input_records !== null && parsed.counts.input_records > 0 && parsed.identity.status === "pass"
      && !parsed.artifacts.some(item => item.status === "missing")
      && parsed.selection_outcome.evidence_refs.every(ref => parsed.artifacts.some(item => item.ref === ref && item.status === "verified")))
      return "zero_candidates" as const
    if (parsed.run.status === "failed" || parsed.run.status === "cancelled" || parsed.failure || parsed.execution_checks.some(item => item.status === "fail")) return "execution_failure" as const
    if (parsed.run.status !== "succeeded") return "incomplete" as const
    if (parsed.counts.selected === 0 && parsed.counts.input_records !== null && parsed.counts.input_records > 0 && parsed.identity.status === "pass" && parsed.artifacts.length > 0 && !parsed.artifacts.some(item => item.status === "missing")) return "zero_candidates" as const
    if (required.some(item => item?.status === "fail")) return "scientific_failure" as const
    if (missing.length || parsed.counts.attempted !== parsed.counts.selected || parsed.counts.execution_succeeded === null || parsed.execution_checks.some(item => item.status === "unknown")) return "incomplete" as const
    if (parsed.counts.scientific_passed === 0) return "scientific_failure" as const
    if (parsed.counts.scientific_passed !== null && parsed.counts.scientific_passed > 0) return "objective_met" as const
    return "incomplete" as const
  })()
  const value = { ...parsed, integrity_errors: unique(errors), schema: "drug-design-observation-v1" as const, classification,
    missing_evidence: unique(missing), evidence_refs: [...refs], new_compute: parsed.origin === "native_execution" }
  return ResearchObservationSchema.parse({ ...value, id: `obs-${researchEvidenceHash(value)}` })
}

/** Recompute the host-derived classification and digest when restoring persisted state. */
export function verifyResearchObservation(input: unknown): ResearchObservation {
  const parsed = ResearchObservationSchema.parse(input)
  const { schema, id, classification, missing_evidence, evidence_refs, new_compute, ...body } = parsed
  const expected = createResearchObservation(body)
  if (researchEvidenceHash(expected) !== researchEvidenceHash(parsed)) throw Error("Research observation content or derived classification was modified")
  return expected
}

export async function captureResearchArtifact(input: { path: string, ref: string, expectedSha256?: string }): Promise<z.infer<typeof ResearchArtifactSchema>> {
  const expected = input.expectedSha256 ? hash.parse(input.expectedSha256) : null
  const filename = path.resolve(input.path)
  const bytes = await readFile(filename).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return null
    throw error
  })
  const sha256 = bytes === null ? null : digest(bytes)
  return ResearchArtifactSchema.parse({ ref: input.ref, path: filename, sha256, expected_sha256: expected, size_bytes: bytes?.length ?? null,
    status: bytes === null ? "missing" : expected === null ? "captured" : expected === sha256 ? "verified" : "mismatch" })
}

// RFC 4180 handling is needed for quoted molecule fields; split(',') silently corrupts identities.
function csv(content: string): Record<string, string>[] {
  const state = { rows: [] as string[][], row: [] as string[], field: "", quoted: false }
  for (let index = 0; index < content.length; index++) {
    const char = content[index]
    if (char === '"') {
      if (state.quoted && content[index + 1] === '"') { state.field += '"'; index++; continue }
      state.quoted = !state.quoted
      continue
    }
    if (char === "," && !state.quoted) { state.row.push(state.field); state.field = ""; continue }
    if (char === "\n" && !state.quoted) { state.row.push(state.field.replace(/\r$/, "")); state.rows.push(state.row); state.row = []; state.field = ""; continue }
    state.field += char
  }
  if (state.quoted) throw Error("Unterminated CSV field")
  if (state.field || state.row.length) { state.row.push(state.field.replace(/\r$/, "")); state.rows.push(state.row) }
  const header = state.rows.shift() ?? []
  if (unique(header).length !== header.length) throw Error("Duplicate CSV column")
  return state.rows.filter(row => row.some(Boolean)).map(row => {
    if (row.length !== header.length) throw Error("CSV row has different field count")
    return Object.fromEntries(header.map((key, index) => [key, row[index]!]))
  })
}

const migration = z.object({
  kind: z.literal("migration-integration-check-not-production-task"), release_id: hash, started_at: z.number().finite(), max_records: count, shortlist: count,
  passed: z.boolean(), input_identity_digest: hash, carsidock_success: count, rtmscore_success: count, output_sha256: hash,
  gates: z.record(z.string(), z.boolean()), runtime_seconds: z.number().nonnegative(),
}).passthrough()

const historicalChecks = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility", "pose_geometry", "registered_objective_acceptance"]

/** Imports only a migration validation: source run identity and historical provenance cannot be overridden. */
export async function importLibraryFirstMigrationObservation(input: {
  reportPath: string
  artifactsDirectory: string
  expectedReportSha256?: string
  expectedHandoffSha256?: string
  expectedReleaseId?: string
  expectedInputIdentityDigest?: string
}): Promise<ResearchObservation> {
  const root = await realpath(input.artifactsDirectory)
  const report = migration.parse(JSON.parse(await readFile(input.reportPath, "utf8")))
  const handoff = z.object({ release_id: hash, gpu_execution_directory: text, artifact_hashes: z.record(z.string(), hash) }).passthrough()
    .parse(JSON.parse(await readFile(path.join(root, "handoff.json"), "utf8")))
  const errors: string[] = []
  const filenames = Object.keys(handoff.artifact_hashes).sort()
  for (const filename of filenames) {
    if (path.isAbsolute(filename) || filename.split(/[\\/]/).includes("..")) throw Error("Artifact path escapes historical evidence directory")
    const resolved = await realpath(path.join(root, filename)).catch((error: NodeJS.ErrnoException) => { if (error.code === "ENOENT") return null; throw error })
    if (resolved && !resolved.startsWith(root + path.sep)) throw Error("Artifact symlink escapes historical evidence directory")
  }
  const artifacts = await Promise.all([
    captureResearchArtifact({ path: input.reportPath, ref: "migration-report", expectedSha256: input.expectedReportSha256 ?? handoff.artifact_hashes["gpu-validation.json"] }),
    captureResearchArtifact({ path: path.join(root, "handoff.json"), ref: "migration-handoff", expectedSha256: input.expectedHandoffSha256 }),
    ...filenames.map(filename => captureResearchArtifact({ path: path.join(root, filename), ref: `migration:${filename}`, expectedSha256: handoff.artifact_hashes[filename] })),
  ])
  if (input.expectedReleaseId && report.release_id !== input.expectedReleaseId) errors.push("unexpected_release_id")
  if (input.expectedInputIdentityDigest && report.input_identity_digest !== input.expectedInputIdentityDigest) errors.push("unexpected_input_identity_digest")
  if (report.release_id !== handoff.release_id) errors.push("handoff_release_mismatch")
  const required = ["prepare/stage2_selection_manifest.json", "prepare/stage2_candidates.csv", "prepare/stage2_candidates.sdf", "carsidock/scores.csv", "rtmscore/scores.csv", "migration-candidates.csv"]
  const absent = required.filter(filename => !artifacts.some(item => item.ref === `migration:${filename}` && item.status !== "missing"))
  if (absent.length) return createResearchObservation({ origin: "historical_import",
    run: { id: path.basename(handoff.gpu_execution_directory), task_id: null, route: "library-first", release_id: report.release_id, input_identity_digest: report.input_identity_digest, started_at: new Date(report.started_at * 1000).toISOString(), status: "unknown", phase: "migration-validation" },
    counts: { input_records: report.max_records, selected: null, attempted: null, execution_succeeded: null, scientific_passed: null }, candidates: [], artifacts,
    identity: { status: "unknown", evidence_refs: [], detail: "Required historical candidate artifacts are absent" }, execution_checks: [], scientific_checks: [], required_scientific_checks: historicalChecks,
    failure: null, integrity_errors: errors, limitations: absent.map(filename => `missing_historical_artifact:${filename}`) })
  const texts = await Promise.all(required.map(filename => readFile(path.join(root, filename), "utf8")))
  const selection = z.object({ status: text, source: z.object({ records_read: count }), counts: z.object({ selected: count }), quality_gates: z.record(z.string(), z.boolean()) }).passthrough().parse(JSON.parse(texts[0]!))
  const prepared = csv(texts[1]!)
  const docked = csv(texts[3]!)
  const scored = csv(texts[4]!)
  const output = csv(texts[5]!)
  const sdf = texts[2]!.split("$$$$").filter(record => record.trim()).map(record => record.match(/>\s*<COMPOUND_ID>[^\n]*\n([^\n]+)/)?.[1]?.trim() ?? "")
  const ids = prepared.map(row => row.compound_id!)
  const sorted = (items: string[]) => JSON.stringify([...items].sort())
  const indices = prepared.map(row => Number(row.input_sdf_index))
  if (prepared.some(row => !row.input_sdf_index?.trim()) || indices.some(index => !Number.isSafeInteger(index) || index < 0) || new Set(indices).size !== indices.length) errors.push("invalid_input_sdf_indices")
  const identity = prepared.map(row => [Number(row.input_sdf_index), row.compound_id!] as const).sort((a, b) => a[0] - b[0])
  if (digest(JSON.stringify(identity)) !== report.input_identity_digest) errors.push("input_identity_digest_mismatch")
  for (const [name, rows] of [["prepared", prepared], ["carsidock", docked], ["rtmscore", scored], ["output", output]] as const) {
    if (rows.some(row => !row.compound_id) || unique(rows.map(row => row.compound_id!)).length !== rows.length) errors.push(`${name}_duplicate_or_missing_identity`)
    if (sorted(rows.map(row => row.compound_id!)) !== sorted(ids)) errors.push(`${name}_identity_denominator_mismatch`)
    for (const row of rows) {
      const original = prepared.find(candidate => candidate.compound_id === row.compound_id)
      if (row.standardized_smiles !== original?.standardized_smiles) errors.push(`${name}_smiles_identity_mismatch`)
      if (row.input_sdf_index !== original?.input_sdf_index) errors.push(`${name}_sdf_index_mismatch`)
    }
  }
  if (sorted(sdf) !== sorted(ids)) errors.push("sdf_identity_denominator_mismatch")
  if (digest(Buffer.from(texts[5]!)) !== report.output_sha256) errors.push("output_sha256_mismatch")
  if (ids.length !== report.shortlist || ids.length !== selection.counts.selected || selection.source.records_read !== report.max_records) errors.push("selection_denominator_mismatch")
  const finite = (value: string | undefined) => Boolean(value?.trim()) && Number.isFinite(Number(value))
  const dockSuccess = docked.filter(row => row.engine_status === "SUCCESS")
  const scoreSuccess = scored.filter(row => row.engine_status === "SUCCESS")
  if (dockSuccess.length !== report.carsidock_success || scoreSuccess.length !== report.rtmscore_success) errors.push("reported_execution_success_mismatch")
  if (dockSuccess.some(row => !finite(row.raw_score)) || scoreSuccess.some(row => !finite(row.raw_score))) errors.push("nonfinite_success_score")
  if (scoreSuccess.some(row => row.source_carsidock_status !== "SUCCESS" || !dockSuccess.some(dock => dock.compound_id === row.compound_id))) errors.push("score_without_successful_pose")
  for (const row of output) {
    const dock = docked.find(item => item.compound_id === row.compound_id)
    const score = scored.find(item => item.compound_id === row.compound_id)
    if (row.carsidock_status !== dock?.engine_status || row.rtmscore_status !== score?.engine_status || Number(row.carsiscore) !== Number(dock?.raw_score) || Number(row.rtmscore) !== Number(score?.raw_score)) errors.push("joined_output_disagrees_with_engine")
  }
  const checks = Object.entries(report.gates).map(([id, passed]) => ({ id, status: passed ? "pass" as const : "fail" as const, evaluator: "migration-integration-check-v1", evidence_refs: ["migration-report"], detail: "Historical execution coverage check; does not establish scientific or biological success" }))
  return createResearchObservation({ origin: "historical_import",
    run: { id: path.basename(handoff.gpu_execution_directory), task_id: null, route: "library-first", release_id: report.release_id, input_identity_digest: report.input_identity_digest, started_at: new Date(report.started_at * 1000).toISOString(), status: report.passed ? "succeeded" : "failed", phase: "migration-validation" },
    counts: { input_records: selection.source.records_read, selected: ids.length, attempted: docked.length, execution_succeeded: scoreSuccess.length, scientific_passed: null },
    candidates: prepared.map(row => ({ id: row.compound_id!, smiles: row.standardized_smiles ?? null, execution_status: scoreSuccess.some(item => item.compound_id === row.compound_id) ? "succeeded" : "failed", scientific_status: "unknown",
      scores: [docked.find(item => item.compound_id === row.compound_id), scored.find(item => item.compound_id === row.compound_id)].flatMap(item => item && item.engine_status === "SUCCESS" && finite(item.raw_score) && ["higher_is_better", "lower_is_better"].includes(item.score_direction!) ? [{ engine: item.engine!, value: Number(item.raw_score), direction: item.score_direction as "higher_is_better" | "lower_is_better" }] : []) })),
    artifacts, identity: { status: errors.length ? "fail" : "pass", evidence_refs: required.map(filename => `migration:${filename}`), detail: "Candidate ID, preserved SMILES, SDF property, table denominators and joined score identity checked; no new chemistry calculation performed" },
    execution_checks: checks, scientific_checks: historicalChecks.map(id => ({ id, status: "unknown", evaluator: null, evidence_refs: [], detail: "This historical migration check did not register or evaluate this scientific requirement" })),
    required_scientific_checks: historicalChecks, failure: report.passed ? null : { category: "engine", detail: "Historical integration report did not pass", evidence_refs: ["migration-report"] }, integrity_errors: errors,
    limitations: ["Historical migration validation; not a new native research task", "Input was the file-order prefix, not a representative sample of the library", "Scores and execution coverage are not evidence of biological activity", "Target construct/residue mapping and pose geometry were not independently accepted in this migration report"] })
}
