import { z } from "zod"
import { discoveryRoute, projectPath } from "./discovery-config"
import { sourceAssessment } from "./candidate-assessment"
import { candidateEvidence } from "./candidate-evidence"

export const sha256 = z.string().regex(/^[a-f0-9]{64}$/)
export const asset = z.discriminatedUnion("storage", [
  z.object({ storage: z.literal("project"), path: projectPath, sha256, mediaType: z.string(), name: z.string() }),
  z.object({ storage: z.literal("artifact"), id: z.string().min(1), version: z.string().min(1), sha256, mediaType: z.string(), name: z.string() }),
])
export const metric = z.object({ id: z.string(), label: z.string(), value: z.number().finite().nullable(), unit: z.string(),
  definition: z.string(), uncertainty: z.object({ kind: z.enum(["SD", "SEM", "CI95"]), value: z.number().nonnegative(), independentN: z.number().int().positive() }).optional() })
export const phase = z.object({ id: z.string().min(1), label: z.string(), state: z.enum(["pending", "running", "completed", "blocked", "failed", "skipped"]),
  status: z.string(), count: z.string(), title: z.string(), detail: z.string(), facts: z.array(z.string()),
  progress: z.object({ completed: z.number().nonnegative(), total: z.number().positive(), unit: z.string() }).refine(v => v.completed <= v.total).optional() })
export const result = z.object({
  schema: z.literal("discovery-result-v1"), project: z.string().min(1), target: z.string().min(1), route: discoveryRoute,
  run: z.string().min(1), collectedAt: z.string().datetime(), evidenceUpdatedAt: z.string().datetime(),
  observation: z.enum(["local-evidence", "scheduler"]), current: z.string(), notice: z.string(),
  stages: z.array(phase), stats: z.array(z.object({ label: z.string(), value: z.string(), unit: z.string(), note: z.string() })),
  candidates: z.array(z.object({ id: z.string().min(1), label: z.string(), smiles: z.string().min(1), featured: z.boolean(),
    badge: z.string(), tone: z.enum(["teal", "amber", "blue", "gray"]), note: z.string(), metrics: z.array(metric),
    scientificEvidence: candidateEvidence.optional(),
    sourceAssessment: sourceAssessment.optional(),
    structures: z.array(z.object({ method: z.string(), format: z.enum(["pdb", "cif", "mae", "maegz", "sdf"]), asset,
      pose: z.number().int().nonnegative().optional(), identity: z.record(z.string(), z.string()) })),
    md: z.object({ state: z.enum(["pending", "running", "ready", "failed"]), summary: z.string(), independentReplicas: z.number().int().nonnegative(),
      metrics: z.array(metric), chartIds: z.array(z.string()) }),
  })),
  analysis: z.object({ state: z.enum(["waiting", "validation", "analysis", "rendering", "report", "ready", "failed"]),
    expectedReplicas: z.number().int().nonnegative(), observedReplicas: z.number().int().nonnegative(), message: z.string() }),
  charts: z.array(z.object({ id: z.string(), candidate: z.string(), kind: z.string(), title: z.string(), caption: z.string(), asset,
    xUnit: z.string(), yUnit: z.string() })),
  reports: z.array(z.object({ title: z.string(), asset })),
  sources: z.array(z.object({ path: z.string(), sha256, updated: z.string().datetime() })),
  // A migration-only projection retains the audited views; new producers use the common fields above.
  legacy: z.object({ kind: z.enum(["denovo", "refinement"]), data: z.unknown() }).optional(),
}).strict().superRefine((value, ctx) => {
  const ids = value.candidates.map(c => c.id)
  const stages = value.stages.map(s => s.id)
  const charts = value.charts.map(c => c.id)
  const problem = (message: string) => ctx.addIssue({ code: "custom", message })
  if (new Set(ids).size !== ids.length || new Set(stages).size !== stages.length || new Set(charts).size !== charts.length) problem("Duplicate identity")
  if (value.current && !stages.includes(value.current)) problem("Unknown current stage")
  for (const chart of value.charts) {
    const candidate = value.candidates.find(c => c.id === chart.candidate)
    if (!candidate) problem("Chart belongs to unknown candidate")
    else if (candidate.md.independentReplicas < 1 || !candidate.md.chartIds.includes(chart.id)) problem("Chart lacks verified candidate binding")
  }
  for (const candidate of value.candidates) for (const id of candidate.md.chartIds)
    if (!value.charts.some(c => c.id === id && c.candidate === candidate.id)) problem("Chart/candidate binding mismatch")
  for (const candidate of value.candidates) {
    const evidence = candidate.scientificEvidence
    if (candidate.sourceAssessment && candidate.sourceAssessment.candidateID !== candidate.id)
      problem("Source assessment candidate identity mismatch")
    if (!evidence) continue
    if (evidence.candidateID !== candidate.id || evidence.smiles !== candidate.smiles)
      problem("Scientific evidence candidate identity mismatch")
    const keys = evidence.measurements.map(item => `${item.scope}:${item.metricID}`)
    if (new Set(keys).size !== keys.length) problem("Duplicate measurement provenance")
    for (const measurement of evidence.measurements) {
      const values = measurement.scope === "candidate" ? candidate.metrics : candidate.md.metrics
      const matches = values.filter(item => item.id === measurement.metricID)
      if (matches.length !== 1 || matches[0].value === null
        || matches[0].unit !== measurement.unit || matches[0].definition !== measurement.definition)
        problem("Measurement provenance differs from reported metric")
    }
  }
})
export type DiscoveryResult = z.infer<typeof result>
export function parseResult(value: unknown, scope: { project: string; target: string; route: string }) {
  const data = result.parse(value)
  if (data.project !== scope.project || data.target !== scope.target || data.route !== scope.route) throw Error("结果与当前项目、靶点或路线不匹配")
  return data
}
export function legacyResult(value: unknown, scope: { project: string; target: string; route: string }) {
  const data = parseResult(value, scope)
  if (!data.legacy || !data.legacy.data || typeof data.legacy.data !== "object") throw Error("Missing audited view projection")
  const view = data.legacy.data as { project: string; route: string; candidates: { row: { compound_id: string; standardized_smiles: string } }[] }
  if (view.project !== data.project || view.route !== data.route || JSON.stringify(view.candidates.map(c => [c.row.compound_id, c.row.standardized_smiles])) !== JSON.stringify(data.candidates.map(c => [c.id, c.smiles]))) throw Error("Audited view identity mismatch")
  return view
}
