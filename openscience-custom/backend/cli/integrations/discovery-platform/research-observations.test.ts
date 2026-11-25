import { afterAll, describe, expect, test } from "bun:test"
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import {
  captureResearchArtifact, createResearchObservation, importLibraryFirstMigrationObservation,
  researchEvidenceHash, ResearchObservationInputSchema, verifyResearchObservation, type ResearchObservationInput,
} from "./research-observations"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const directory = path.join(workspace, "reports/layout-migration-20261012/gpu-final")
const reportPath = path.join(workspace, "reports/layout-migration-20261012/gpu-validation.json")
const scratch = await mkdtemp(path.join(tmpdir(), "research-observations-"))
afterAll(() => rm(scratch, { recursive: true, force: true }))

async function fixture(): Promise<ResearchObservationInput> {
  // Deliberately synthetic for branch replay. It can never masquerade as historical/new computation.
  const filename = path.join(scratch, "synthetic-evaluator-result.json")
  await writeFile(filename, JSON.stringify({ kind: "synthetic-replay-fixture", candidate: "fixture-candidate", gate: "pose_geometry", status: "pass" }))
  const artifact = await captureResearchArtifact({ path: filename, ref: "fixture-evaluator" })
  return {
    origin: "replay_fixture",
    run: { id: "fixture-run", task_id: null, route: "library-first", release_id: "1".repeat(64), input_identity_digest: "2".repeat(64), started_at: "2026-10-13T00:00:00.000Z", status: "succeeded", phase: "evaluation" },
    counts: { input_records: 20000, selected: 1, attempted: 1, execution_succeeded: 1, scientific_passed: 1 },
    candidates: [{ id: "fixture-candidate", smiles: "C=CC(=O)N", execution_status: "succeeded", scientific_status: "pass", scores: [] }],
    artifacts: [artifact],
    identity: { status: "pass", evidence_refs: ["fixture-evaluator"], detail: "Synthetic replay identity" },
    execution_checks: [{ id: "engine_completion", status: "pass", evaluator: "fixture-evaluator-v1", evidence_refs: ["fixture-evaluator"], detail: "Synthetic successful engine" }],
    scientific_checks: [{ id: "pose_geometry", status: "pass", evaluator: "fixture-evaluator-v1", evidence_refs: ["fixture-evaluator"], detail: "Synthetic acceptance" }],
    required_scientific_checks: ["pose_geometry"], failure: null, integrity_errors: [], limitations: ["Synthetic fixture, not a chemistry or GPU result"],
  }
}

describe("historical Library-first evidence import", () => {
  test("checks the real 20k / 5 GPU record while keeping unverified science unknown", async () => {
    const value = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: directory,
      expectedReportSha256: "17f52a7c432f5febef486cb5710fabb42931c102baa79c17cec9e739b1d1aade",
      expectedReleaseId: "5d6e9c48825c18668bec9ceeddf3502482197258becc4a54daed8fbcc8491910",
      expectedInputIdentityDigest: "b4d414caf8a2c29a2e6872c04d0620681041d043b554dfd3bf6d9ed729822673" })
    expect(value.classification).toBe("incomplete")
    expect(value.origin).toBe("historical_import")
    expect(value.new_compute).toBe(false)
    expect(value.run.task_id).toBeNull()
    expect(value.run.id).toBe("migration-validation-personal-v1")
    expect(value.counts).toEqual({ input_records: 20000, selected: 5, attempted: 5, execution_succeeded: 5, scientific_passed: null })
    expect(value.identity.status).toBe("pass")
    expect(value.integrity_errors).toEqual([])
    expect(value.candidates).toHaveLength(5)
    expect(value.candidates.every(item => item.scores.length === 2 && item.scientific_status === "unknown")).toBe(true)
    expect(value.artifacts.filter(item => item.status === "verified")).toHaveLength(30)
    expect(value.missing_evidence).toContain("scientific_check:target_construct_and_residue_mapping")
    expect(value.missing_evidence).toContain("scientific_check:pose_geometry")
    expect(verifyResearchObservation(JSON.parse(JSON.stringify(value)))).toEqual(value)
  })

  test("reimport is deterministic and does not create another compute event", async () => {
    const first = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: directory })
    const second = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: directory })
    expect(first).toEqual(second)
    expect(first.new_compute).toBe(false)
  })

  test("expected release and input mismatches fail integrity", async () => {
    const value = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: directory, expectedReleaseId: "0".repeat(64), expectedInputIdentityDigest: "0".repeat(64) })
    expect(value.classification).toBe("integrity_failure")
    expect(value.integrity_errors).toContain("unexpected_release_id")
    expect(value.integrity_errors).toContain("unexpected_input_identity_digest")
  })

  test("input identity digest is recomputed from SDF indices and IDs, not trusted from a report", async () => {
    const filename = path.join(scratch, "wrong-identity-report.json")
    const report = JSON.parse(await readFile(reportPath, "utf8"))
    await writeFile(filename, JSON.stringify({ ...report, input_identity_digest: "0".repeat(64) }))
    const value = await importLibraryFirstMigrationObservation({ reportPath: filename, artifactsDirectory: directory })
    expect(value.classification).toBe("integrity_failure")
    expect(value.integrity_errors).toContain("input_identity_digest_mismatch")
  })

  test("modified joined candidate molecule fails hash and identity validation", async () => {
    const copied = path.join(scratch, "changed-output")
    await cp(directory, copied, { recursive: true })
    const filename = path.join(copied, "migration-candidates.csv")
    const original = await readFile(filename, "utf8")
    await writeFile(filename, original.replaceAll("C=CC(=O)Nc1cccc(B(O)O)c1", "CC"))
    const value = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: copied })
    expect(value.classification).toBe("integrity_failure")
    expect(value.integrity_errors).toContain("artifact_hash_mismatch")
    expect(value.integrity_errors).toContain("output_smiles_identity_mismatch")
    expect(value.integrity_errors).toContain("output_sha256_mismatch")
  })

  test("missing candidate artifacts produce an explicit incomplete observation", async () => {
    const copied = path.join(scratch, "missing-output")
    await cp(directory, copied, { recursive: true })
    await rm(path.join(copied, "rtmscore/scores.csv"))
    const value = await importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: copied })
    expect(value.classification).toBe("incomplete")
    expect(value.identity.status).toBe("unknown")
    expect(value.counts.selected).toBeNull()
    expect(value.artifacts.find(item => item.ref === "migration:rtmscore/scores.csv")?.status).toBe("missing")
  })

  test("manifest traversal paths are rejected before artifact reads", async () => {
    const copied = path.join(scratch, "traversal")
    await cp(directory, copied, { recursive: true })
    const filename = path.join(copied, "handoff.json")
    const receipt = JSON.parse(await readFile(filename, "utf8"))
    receipt.artifact_hashes["../outside"] = "0".repeat(64)
    await writeFile(filename, JSON.stringify(receipt))
    await expect(importLibraryFirstMigrationObservation({ reportPath, artifactsDirectory: copied })).rejects.toThrow("escapes")
  })
})

describe("host-owned observation classification and restoration", () => {
  test("explicit evidenced scientific success permits objective_met for a replay only", async () => {
    const value = createResearchObservation(await fixture())
    expect(value.classification).toBe("objective_met")
    expect(value.new_compute).toBe(false)
    expect(value.origin).toBe("replay_fixture")
    expect(value.missing_evidence).toEqual([])
  })

  test("a successful empty selection has a bounded zero_candidates outcome", async () => {
    const input = await fixture()
    input.candidates = []
    input.counts = { input_records: 20000, selected: 0, attempted: 0, execution_succeeded: 0, scientific_passed: 0 }
    input.scientific_checks = []
    const value = createResearchObservation(input)
    expect(value.classification).toBe("zero_candidates")
    expect(value.counts.input_records).toBe(20000)
    expect(value.missing_evidence).toContain("scientific_check:pose_geometry")
  })

  test("structural failure is scientific_failure, not an engine failure", async () => {
    const input = await fixture()
    input.scientific_checks[0]!.status = "fail"
    input.candidates[0]!.scientific_status = "fail"
    input.counts.scientific_passed = 0
    const value = createResearchObservation(input)
    expect(value.classification).toBe("scientific_failure")
    expect(value.run.status).toBe("succeeded")
    expect(value.counts.execution_succeeded).toBe(1)
  })

  test("engine failure preserves failure category and does not imply molecular inactivity", async () => {
    const input = await fixture()
    input.run.status = "failed"
    input.failure = { category: "environment", detail: "Replay: Python ABI mismatch before scoring", evidence_refs: ["fixture-evaluator"] }
    input.candidates[0]!.execution_status = "failed"
    input.candidates[0]!.scientific_status = "unknown"
    input.counts.execution_succeeded = 0
    input.counts.scientific_passed = null
    input.scientific_checks = []
    const value = createResearchObservation(input)
    expect(value.classification).toBe("execution_failure")
    expect(value.failure?.category).toBe("environment")
    expect(value.counts.scientific_passed).toBeNull()
  })

  test("running jobs and missing scientific evidence stay incomplete", async () => {
    const input = await fixture()
    input.run.status = "running"
    expect(createResearchObservation(input).classification).toBe("incomplete")
    input.run.status = "succeeded"
    input.scientific_checks = []
    expect(createResearchObservation(input).classification).toBe("incomplete")
  })

  test("unknown individual scientific checks cannot be converted to zero passing candidates", async () => {
    const input = await fixture()
    input.candidates[0]!.scientific_status = "unknown"
    input.counts.scientific_passed = 0
    const value = createResearchObservation(input)
    expect(value.classification).toBe("incomplete")
    expect(value.missing_evidence).toContain("candidate_scientific_evaluation")
  })

  test("unknown or fabricated evidence references cannot justify scientific success", async () => {
    const input = await fixture()
    input.scientific_checks[0]!.evidence_refs = ["fabricated-result"]
    const value = createResearchObservation(input)
    expect(value.classification).toBe("integrity_failure")
    expect(value.integrity_errors).toContain("unresolved_evidence_reference")
  })

  test("duplicate candidate identity and changed denominators are integrity failures", async () => {
    const input = await fixture()
    input.candidates.push(input.candidates[0]!)
    input.counts.attempted = 3
    const value = createResearchObservation(input)
    expect(value.classification).toBe("integrity_failure")
    expect(value.integrity_errors).toContain("duplicate_candidate_identity")
    expect(value.integrity_errors).toContain("candidate_denominator_mismatch")
    expect(value.integrity_errors).toContain("invalid_denominator_order")
  })

  test("tampering with a persisted outcome or origin is rejected", async () => {
    const value = createResearchObservation(await fixture())
    expect(() => verifyResearchObservation({ ...value, classification: "zero_candidates" })).toThrow("modified")
    expect(() => verifyResearchObservation({ ...value, origin: "native_execution" })).toThrow("modified")
    expect(verifyResearchObservation(value)).toEqual(value)
  })

  test("NaN scores and unknown fields are schema errors, not missing successes", async () => {
    const input = await fixture()
    input.candidates[0]!.scores = [{ engine: "fixture", value: NaN, direction: "higher_is_better" }]
    expect(() => createResearchObservation(input)).toThrow()
    expect(ResearchObservationInputSchema.safeParse({ ...await fixture(), expected_observation: "Model predicted success" }).success).toBe(false)
  })

  test("artifact reads verify expected content and persist missing files without inventing data", async () => {
    const captured = await captureResearchArtifact({ path: reportPath, ref: "report" })
    const checked = await captureResearchArtifact({ path: reportPath, ref: "report", expectedSha256: captured.sha256! })
    expect(checked.status).toBe("verified")
    const changed = await captureResearchArtifact({ path: reportPath, ref: "report", expectedSha256: "0".repeat(64) })
    expect(changed.status).toBe("mismatch")
    const missing = await captureResearchArtifact({ path: path.join(scratch, "absent-file"), ref: "absent" })
    expect(missing.status).toBe("missing")
    expect(missing.sha256).toBeNull()
  })

  test("canonical evidence hashes do not depend on object key order", () => {
    expect(researchEvidenceHash({ b: 2, a: 1 })).toBe(researchEvidenceHash({ a: 1, b: 2 }))
    expect(researchEvidenceHash([1, 2])).not.toBe(researchEvidenceHash([2, 1]))
  })
})
