import { expect, test } from "bun:test"
import { deNovoSourceAssessment, sourceAssessment, assessmentLabel } from "@synsci/util/candidate-assessment"
import { candidateHandoff } from "@synsci/util/candidate-handoff"

const evidence = { path: "/fixture/verification.json", sha256: "a".repeat(64) }
test("source QC distinguishes evaluation errors, unavailable identity and actual rule rejection", () => {
  for (const [failures, smiles, hash, status] of [
    [["evaluation_error"], null, "b".repeat(64), "NOT_EVALUATED"],
    [["unparseable_molecule"], null, null, "UNKNOWN"],
    [["unparseable_molecule"], null, "b".repeat(64), "UNKNOWN"],
    [["protein_clashes"], "C", "b".repeat(64), "REJECTED"],
    [["relaxation_not_converged"], "C", "b".repeat(64), "REJECTED"],
    [[], "C", "b".repeat(64), "PASS"],
  ] as const) {
    const value = deNovoSourceAssessment({ candidate_id: "candidate", failures: [...failures],
      smiles, source_sha256: hash, structural_qc_passed: status === "PASS" }, evidence)
    expect(value.status).toBe(status)
    expect(value.failures).toEqual([...failures])
    expect(value.physicalValidity).toBe("not_assessed_by_reader")
    expect(value.independentEvaluation).toBe("NOT_PERFORMED")
    expect(value.evidence).toEqual(evidence)
    expect(sourceAssessment.parse(JSON.parse(JSON.stringify(value)))).toEqual(value)
    expect(assessmentLabel(value)).not.toBe("结构 QC 失败")
    if (status !== "PASS") expect(() => sourceAssessment.parse({ ...value, status: "PASS" })).toThrow()
  }
})

test("the shared handoff retains QC evidence and checks its candidate identity", () => {
  const assessment = deNovoSourceAssessment({ candidate_id: "candidate", failures: ["protein_clashes"],
    smiles: "C", source_sha256: "b".repeat(64), structural_qc_passed: false }, evidence)
  const missing = { state: "missing", reason: "Fixture has no scientific evidence" }
  const value = {
    schema: "candidate-evaluation-handoff-v1",
    provenance: { recordID: "record", study: "study", route: "de-novo", run: "run",
      sha256: "a".repeat(64), mode: "native-generation", candidateID: "candidate" },
    chemicalIdentity: { state: "reported", smiles: "C", validation: "not_checked_by_handoff" },
    sourceEvidence: null, sourceAssessment: assessment, poseProvenance: missing,
    warhead: { state: "requested_not_verified", requested: "acrylamide", reactionAtoms: missing },
    target: { requested: { id: "egfr", gene: "EGFR", uniprot: "P00533", taxon: 9606,
      site: { chain: "A", number: 797, residue: "CYS", atom: "SG", numbering: "canonical" },
      receptor: { reference: "fixture" } }, numberingMap: missing },
    poses: [], metrics: [], missingEvidence: ["pose"], decision: "not_assessed",
  }
  expect(candidateHandoff.parse(value).sourceAssessment).toEqual(assessment)
  expect(() => candidateHandoff.parse({ ...value, sourceAssessment: { ...assessment, candidateID: "different" } })).toThrow("identity")
  const legacy = { ...value, sourceAssessment: undefined }
  expect(candidateHandoff.parse(legacy).decision).toBe("not_assessed")
})
