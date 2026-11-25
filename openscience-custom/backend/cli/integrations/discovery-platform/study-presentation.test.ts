import { expect, test } from "bun:test"
import path from "node:path"
import { studyStructure } from "../../../../frontend/workspace/src/discovery/study-structure"
import { studyReport } from "@synsci/util/discovery-study-report"
import { candidateStructures } from "../../../../frontend/workspace/src/artifacts/candidate-structure"
import type { StudyCandidateSource, StudyOverview } from "@synsci/util/discovery-study"
import { result } from "@synsci/util/discovery-result"
import { deNovoSourceAssessment } from "@synsci/util/candidate-assessment"

test("all real historical structures adapt to explicit existing-viewer references", async () => {
  for (const route of ["library-first", "ligand-first", "de-novo"] as const) {
    const data = result.parse(await Bun.file(path.resolve(import.meta.dir, `../../../../results/discovery/platform/${route}.json`)).json())
    for (const candidate of data.candidates) {
      const source: StudyCandidateSource = { recordID: candidate.id, study: "test", route, sourceRun: data.run, sourceSha256: "a".repeat(64), mode: "historical-reuse", candidate }
      candidate.structures.forEach((ref, index) => {
        const view = studyStructure(source, index)
        const resolved = candidateStructures(view.row)[0]
        expect(ref.method).toBe(resolved.method)
        expect(resolved.sha256).toBe(ref.asset.sha256)
        expect(resolved.pose).toBe(ref.pose)
      })
      const bad = structuredClone(source)
      bad.candidate.structures[0].identity.compound_id = "different-candidate"
      expect(() => studyStructure(bad, 0)).toThrow("身份")
    }
  }
})

test("readable report distinguishes history, missing scores and untested execution", async () => {
  const view: StudyOverview = { schema: "discovery-study-overview-v1", newComputeSubmitted: false,
    plan: { schema: "discovery-study-v1", id: "study-00000000-0000-0000-0000-000000000000", project: "test", createdAt: "2026-10-10T00:00:00Z", title: "Test",
      target: { id: "test", gene: "TEST", taxon: 9606, uniprot: "TEST", site: { chain: "B", residue: "CYS", number: 909, atom: "SG", numbering: "canonical" }, receptor: { reference: "test" } },
      hypothesis: { state: "pending", rationale: "", evidence: [] }, routes: { "library-first": { selected: true, mode: "reuse", inputs: {} }, "ligand-first": { selected: false, mode: "reuse", inputs: {} }, "de-novo": { selected: false, mode: "reuse", inputs: {} } } },
    routes: [], candidates: [], pool: { policy: "test", rdkitVersion: null, ranked: false, sourceCount: 0, groups: [] } }
  const report = studyReport(view)
  expect(report).toContain("未提交新科研计算")
  expect(report).toContain("未提供的指标不是零")
  expect(report).toContain("B:CYS909:SG")
  expect(report).toContain("输入声明不能替代真实执行")
  const data = result.parse(await Bun.file(path.resolve(import.meta.dir, "../../../../results/discovery/platform/de-novo.json")).json())
  const candidate = { ...data.candidates[0], metrics: [{ id: "missing", label: "缺失角度", value: null,
    unit: "degree", definition: "本夹具未提供测量" }], scientificEvidence: undefined,
    sourceAssessment: deNovoSourceAssessment({ candidate_id: data.candidates[0].id,
      structural_qc_passed: false, smiles: data.candidates[0].smiles, source_sha256: "b".repeat(64),
      failures: ["evaluation_error"] }, { path: "/fixture/verification.json", sha256: "a".repeat(64) }) }
  expect(result.parse({ ...data, candidates: [candidate], charts: [] }).candidates[0].sourceAssessment).toEqual(candidate.sourceAssessment)
  expect(() => result.parse({ ...data, candidates: [{ ...candidate,
    sourceAssessment: { ...candidate.sourceAssessment, candidateID: "different" } }], charts: [] })).toThrow("identity")
  const source: StudyCandidateSource = { recordID: candidate.id, study: view.plan.id, route: "de-novo",
    sourceRun: "native-run", sourceSha256: "a".repeat(64), mode: "native-generation", candidate }
  const native = studyReport({ ...view, candidates: [source], pool: { ...view.pool, sourceCount: 1,
    groups: [{ id: "one", identity: { state: "reported", canonical: null, reason: "fixture" }, sources: [source] }] } })
  expect(native).toContain("解释状态：NOT_EVALUATED；评价过程：ERROR")
  expect(native).toContain("evaluation_error")
  expect(native).toContain("structural_evaluation_error")
  expect(native).toContain("缺失角度：未提供")
  expect(native).toContain("仅核验回执一致性")
  expect(native).not.toContain("本研究未提交新科研计算")
})
