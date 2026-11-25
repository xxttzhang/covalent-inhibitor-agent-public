// Display-only CSV parsing. Preserve file order, identity, scores and failures.
export function candidateTable(text: string) {
  if (text.length > 8 * 1024 * 1024) return
  const rows: string[][] = []
  let row: string[] = []
  let field = ""
  let quoted = false
  let closed = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (quoted) {
      if (c !== '"') { field += c; continue }
      if (text[i + 1] === '"') { field += '"'; i++; continue }
      quoted = false; closed = true; continue
    }
    if (c === '"') {
      if (field || closed) return
      quoted = true; continue
    }
    if (c === "," || c === "\n" || c === "\r") {
      row.push(field); field = ""; closed = false
      if (c === ",") continue
      if (c === "\r" && text[i + 1] === "\n") i++
      rows.push(row); row = []
      if (rows.length > 2001) return
      continue
    }
    if (closed) return
    field += c
  }
  if (quoted) return
  if (field || closed || row.length) { row.push(field); rows.push(row) }
  const header = rows.shift()?.map((s, i) => i === 0 ? s.replace(/^\uFEFF/, "") : s)
  if (!header || new Set(header).size !== header.length || rows.length > 2000) return
  if (!header.includes("compound_id") || !rows.length || rows.some((r) => r.length !== header.length)) return
  const smiles = ["standardized_smiles", "smiles", "original_smiles"].find((key) => header.includes(key))
  if (!smiles) return
  return { header, smiles, rows: rows.map((values) => Object.fromEntries(header.map((key, i) => [key, values[i]]))) }
}

const overviewScopes = new Set([
  "historical_two_candidate_reuse", "new_single_covdock_upstream_reused",
  "library_first_refinement_display", "candidate-results-v1", "candidate-results-v2",
])

const scientificParetoPolicies = new Set([
  "library-first-pareto-v2.0",
  "ligand-first-pareto-v1.0",
])

export function isScientificPareto(rows: Record<string, string>[]) {
  return rows.length > 0 && rows.every((row) => scientificParetoPolicies.has(row.ranking_policy))
}

export function paretoPolicyLabel(policy?: string) {
  if (policy === "library-first-pareto-v2.0") return "Library-first Pareto v2.0"
  if (policy === "ligand-first-pareto-v1.0") return "Ligand-first Pareto v1.0"
  return "未登记排序策略"
}

type CandidateStageState = "COMPLETED" | "COMPLETED_WITH_WARNINGS" | "FAILED_OR_PARTIAL" | "NOT_RECORDED"
export type CandidateStageSummary = {
  key: "af3" | "covdock" | "independent_evaluation" | "pareto"
  label: string
  state: CandidateStageState
  completed: number
  total: number
}

function terminalSummary(
  rows: Record<string, string>[],
  key: CandidateStageSummary["key"],
  label: string,
  values: string[],
  success: Set<string>,
  warning: Set<string> = new Set(),
) : CandidateStageSummary {
  const normalized = values.map((value) => value?.trim().toUpperCase() ?? "")
  const known = normalized.filter((value) => success.has(value) || warning.has(value) || ["FAILED", "FAIL", "NOT_RUN"].includes(value))
  const completed = normalized.filter((value) => success.has(value) || warning.has(value)).length
  const state: CandidateStageState = known.length !== rows.length ? "NOT_RECORDED"
    : normalized.some((value) => ["FAILED", "FAIL", "NOT_RUN"].includes(value)) ? "FAILED_OR_PARTIAL"
      : normalized.some((value) => warning.has(value)) ? "COMPLETED_WITH_WARNINGS" : "COMPLETED"
  return { key, label, state, completed, total: rows.length }
}

export function candidateStageSummary(rows: Record<string, string>[]): CandidateStageSummary[] {
  if (!rows.length) return []
  const af3 = terminalSummary(rows, "af3", "AF3–mPAE", rows.map((row) => row.af3_status), new Set(["SUCCESS"]))
  const covdock = terminalSummary(rows, "covdock", "CovDock-LO", rows.map((row) => row.covdock_status), new Set(["SUCCESS"]))
  const independent = terminalSummary(rows, "independent_evaluation", "独立评价", rows.map((row) => row.independent_gate), new Set(["PASS"]), new Set(["WARN"]))
  const paretoComplete = isScientificPareto(rows) && rows.every((row) => Boolean(row.final_rank?.trim() || row.pareto_exclusion_reason?.trim()))
  const pareto: CandidateStageSummary = {
    key: "pareto", label: "Pareto 排名",
    state: paretoComplete ? "COMPLETED" : "NOT_RECORDED",
    completed: paretoComplete ? rows.length : 0, total: rows.length,
  }
  return [af3, covdock, independent, pareto]
}

export function candidatePresentation(rows: Record<string, string>[]) {
  if (rows.length > 0 && rows.every((row) => row.route === "de-novo"
    && ["denovo-covdock-comparison-v1", "candidate-results-v2"].includes(row.result_scope))) return "de-novo" as const
  if (rows.length > 0 && rows.length <= 12
    && rows.every((row) => row.route !== "de-novo" && overviewScopes.has(row.result_scope))) return "overview" as const
  return "table" as const
}
