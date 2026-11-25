import { renderTemplate, type RoutePresentation } from "@synsci/util/discovery-config"
import { candidateEvidence } from "@synsci/util/candidate-evidence"
import { z } from "zod"

const metric = z.object({ mean: z.number().finite(), sd_between_replicas: z.number().nonnegative(), replica_estimates: z.array(z.number().finite()) })
const system = z.object({ sampling_assessment: z.string(), replica_estimates: z.record(z.string(), metric), equilibrium_proven: z.boolean() })
export const workbench = z.object({
  schema: z.literal("discovery-workbench-v1"), route: z.literal("de-novo"), project: z.string(), session: z.string(), task: z.string(),
  collectedAt: z.string().datetime(), evidenceUpdatedAt: z.string().datetime(), observation: z.literal("local-evidence"), refreshSeconds: z.number(),
  generation: z.object({ state: z.string(), updated: z.string(), slots: z.number(), passed: z.number(), relaxedPassed: z.number() }),
  docking: z.object({ completed: z.boolean(), candidates: z.number(), poses: z.number(), passed: z.number(), controlRmsd: z.number() }),
  md: z.object({ replicas: z.number(), totalNs: z.number(), frames: z.number(), engine: z.string(), forcefield: z.string(), systems: z.number(), assessment: z.string(), control: system }),
  amber: z.object({ sourceSystems: z.number(), productionStarted: z.boolean(), forcefield: z.string(), probes: z.array(z.object({ system: z.string(), status: z.string(), updated: z.string(), scope: z.string(), path: z.string() })) }),
  candidates: z.array(z.object({ row: z.record(z.string(), z.string()), molecularWeight: z.number().nullable(), logP: z.number().nullable(),
    scientificEvidence: candidateEvidence.optional(),
    md: z.object({ assessment: z.string(), rmsd: metric, contacts: metric.optional(), replicas: z.number() }).nullable() })),
  reports: z.array(z.object({ id: z.string(), version: z.string(), name: z.string(), title: z.string(), sha256: z.string() })),
  sources: z.array(z.object({ path: z.string(), sha256: z.string(), updated: z.string() })),
})
export type Workbench = z.infer<typeof workbench>
export type Candidate = Workbench["candidates"][number]
export const format = (value: string | number | null | undefined, digits = 2) => value !== null && value !== undefined && String(value).trim() && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—"
export const time = (value: string) => new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai" })
export const stale = (value: string, now = Date.now()) => !Number.isFinite(Date.parse(value)) || now - Date.parse(value) > 60_000
export function stages(data: Workbench, presentation: RoutePresentation) {
  const probe = [...data.amber.probes].sort((a, b) => b.updated.localeCompare(a.updated))[0]
  const probeLabel = !probe ? "等待参数探针" : probe.status === "CAPPED_ADDUCT_AMBER_PROBE_PASSED" ? "封端探针通过，待完整拓扑" : probe.status === "RUNNING" ? "探针状态待核对" : "最近探针未通过，待修复"
  return renderTemplate(presentation.stages, { ...data, probeLabel })
}
export function recommendation(id: string, presentation: RoutePresentation) {
  return presentation.recommendations[id] ?? { label: "待评价", detail: "当前候选尚未登记研究建议。", tone: "gray" }
}
