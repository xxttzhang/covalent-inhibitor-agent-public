import { assessmentLabel } from "@synsci/util/candidate-assessment"
import { createHash } from "node:crypto"
import type { DiscoveryStudy, StudyCandidateSource } from "@synsci/util/discovery-study"
import { readDeNovoPlan } from "./study-denovo-plan"
import { readDeNovoTask, deNovoTasks } from "../../src/tool/denovo-task"
import { readDeNovoResults } from "../../src/tool/denovo-results"

export async function studyDeNovoCandidates(directory: string, study: DiscoveryStudy): Promise<StudyCandidateSource[]> {
  if (!study.routes["de-novo"].selected || study.routes["de-novo"].mode !== "new") return []
  const plan = await readDeNovoPlan(directory, study)
  if (!plan) return []
  const expected = { directory, study: study.id, request: plan.plan.request }
  const id = String(plan.plan.request.id)
  const task = await readDeNovoTask(deNovoTasks, id, expected)
  if (task?.state !== "COMPLETED") return []
  const results = await readDeNovoResults(deNovoTasks, id, expected)
  if (!results.candidates || !results.provenance) throw Error("Completed task has no candidate receipt")
  return results.candidates.flatMap(row => {
    // Unparseable slots remain in the workflow evaluation ledger, never invented as molecules.
    if (!row.smiles) return []
    const recordID = createHash("sha256").update(JSON.stringify([study.id, id, row.candidate_id])).digest("hex")
    return [{ recordID, study: study.id, route: "de-novo" as const, sourceRun: id,
      sourceSha256: results.provenance.verification_sha256, mode: "native-generation",
      candidate: { id: row.candidate_id, label: `Slot ${row.slot} · ${row.representation}`,
        smiles: row.smiles, featured: false,
        badge: assessmentLabel(row.sourceAssessment), sourceAssessment: row.sourceAssessment,
        tone: "gray" as const, note: row.failures.join("; "),
        metrics: row.metrics, scientificEvidence: row.scientificEvidence,
        structures: [], md: { state: "pending" as const, summary: "本任务尚未运行 MD",
          independentReplicas: 0, metrics: [], chartIds: [] } } }]
  })
}
