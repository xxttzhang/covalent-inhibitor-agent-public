import { expect, test } from "bun:test"
import { candidatePresentation, candidateStageSummary, candidateTable, isScientificPareto, paretoPolicyLabel } from "./candidate-table"

test("preserves quoted CSV identities, multiline evidence, zero scores and original order", () => {
  const result = candidateTable('\uFEFFcompound_id,standardized_smiles,covdock_score,covdock_gate_reason\r\n"id,2",CCO,0,"missing\nconsistency"\r\n"id""1",C,-2,B2\r\n')!
  expect(result.rows).toHaveLength(2)
  expect(result.rows[0]).toEqual({ compound_id: "id,2", standardized_smiles: "CCO", covdock_score: "0", covdock_gate_reason: "missing\nconsistency" })
  expect(result.rows[1].compound_id).toBe('id"1')
})
test("noncandidate/malformed tables fall back without inventing structure", () => {
  for (const text of ["x,y\n1,2", "compound_id,smiles\na", 'compound_id,smiles\n"bad,C',
    "compound_id,smiles,smiles\na,C,C", 'compound_id,smiles\na,"C"junk']) expect(candidateTable(text)).toBeUndefined()
  expect(candidateTable("compound_id,smiles\na,\n")!.rows[0].smiles).toBe("")
})
test("preferred structure column is explicit and failed candidates remain", () => {
  const result = candidateTable("compound_id,original_smiles,standardized_smiles,engine_status\na,CO,C,FAILED\nb,CC,CC,SUCCESS")!
  expect(result.smiles).toBe("standardized_smiles")
  expect(result.rows[0].engine_status).toBe("FAILED")
  expect(result.rows).toHaveLength(2)
})

test("v2 routes use the correct specialized workbench without overlap", () => {
  expect(candidatePresentation([{ route: "de-novo", result_scope: "candidate-results-v2" }])).toBe("de-novo")
  expect(candidatePresentation([{ route: "ligand-first", result_scope: "candidate-results-v2" }])).toBe("overview")
  expect(candidatePresentation([{ route: "library-first", result_scope: "candidate-results-v2" }])).toBe("overview")
  expect(candidatePresentation([{ route: "de-novo", result_scope: "candidate-results-v1" }])).toBe("table")
  expect(candidatePresentation([])).toBe("table")
})

test("both registered route-specific Pareto policies enable ranked workbench evidence", () => {
  expect(isScientificPareto([{ ranking_policy: "library-first-pareto-v2.0" }])).toBe(true)
  expect(isScientificPareto([{ ranking_policy: "ligand-first-pareto-v1.0" }])).toBe(true)
  expect(isScientificPareto([{ ranking_policy: "ligand-first-pareto-v1.0" }, { ranking_policy: "" }])).toBe(false)
  expect(isScientificPareto([])).toBe(false)
  expect(paretoPolicyLabel("ligand-first-pareto-v1.0")).toBe("Ligand-first Pareto v1.0")
})

test("unified workbench derives honest four-stage progress from delivered evidence", () => {
  const rows = [0, 1, 2].map((index) => ({
    af3_status: "SUCCESS", covdock_status: index === 2 ? "FAILED" : "SUCCESS",
    independent_gate: index === 1 ? "WARN" : index === 2 ? "FAIL" : "PASS",
    ranking_policy: "ligand-first-pareto-v1.0",
    final_rank: index < 2 ? String(index + 1) : "",
    pareto_exclusion_reason: index === 2 ? "physical_or_independent_gate_FAIL_or_unknown" : "",
  }))
  expect(candidateStageSummary(rows)).toEqual([
    { key: "af3", label: "AF3–mPAE", state: "COMPLETED", completed: 3, total: 3 },
    { key: "covdock", label: "CovDock-LO", state: "FAILED_OR_PARTIAL", completed: 2, total: 3 },
    { key: "independent_evaluation", label: "独立评价", state: "FAILED_OR_PARTIAL", completed: 2, total: 3 },
    { key: "pareto", label: "Pareto 排名", state: "COMPLETED", completed: 3, total: 3 },
  ])
  expect(candidateStageSummary([{ af3_status: "" }])[0].state).toBe("NOT_RECORDED")
})
