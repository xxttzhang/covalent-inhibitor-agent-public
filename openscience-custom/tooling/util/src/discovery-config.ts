import { z } from "zod"

export const discoveryRoute = z.enum(["library-first", "ligand-first", "de-novo"])
export const projectPath = z.string().min(1).max(2000).refine(value =>
  !value.startsWith("/") && !value.includes("\\") && !value.split("/").some(part => ["", ".", ".."].includes(part)), "Expected a project-relative path")
const phase = z.object({ id: z.string(), label: z.string(), state: z.string(), status: z.string(), count: z.string(), title: z.string(), detail: z.string(), facts: z.array(z.string()) })
export const routePresentation = z.object({
  view: z.enum(["standard", "denovo", "refinement"]).default("standard"),
  name: z.string(), title: z.string(), subtitle: z.string(), filter: z.string(),
  bond: z.object({ chain: z.string(), residue: z.string(), number: z.number().int(), atom: z.string(), element: z.string(), ligandAtom: z.string(), neighborAtom: z.string() }).optional(),
  inputPath: projectPath.optional(), dataPath: projectPath.optional(), defaultStage: z.string().default(""),
  featured: z.array(z.string()).default([]), stages: z.array(phase).default([]),
  stats: z.array(z.object({ label: z.string(), value: z.string(), unit: z.string(), note: z.string() })).default([]),
  recommendations: z.record(z.string(), z.object({ label: z.string(), detail: z.string(), tone: z.string() })).default({}),
  evidenceHeading: z.string().default("当前证据"), evidenceNote: z.string().default("结构分别查看，未作对齐叠合。"),
  md: z.object({ statePath: projectPath, artifactPrefix: projectPath, expectedReplicas: z.number().int().positive(),
    durationNote: z.string(), contactNote: z.string(), samplingNote: z.string(), footer: z.string(), rmsfNote: z.string(),
    sourceNote: z.string(), reportName: z.string(), reportLabel: z.string() }).optional(),
})
export const discoveryProject = z.object({
  schema: z.literal("discovery-project-v1"), project: z.string().min(1),
  target: z.object({ id: z.string().min(1), name: z.string().min(1), label: z.string().min(1),
    identity: z.object({ gene: z.string().min(1), uniprot: z.string().min(1), taxon: z.number().int().positive(),
      numbering: z.enum(["canonical", "structure"]) }).strict().optional(),
    site: z.object({ chain: z.string(), residue: z.string(), number: z.number().int(), atom: z.string() }).optional() }).nullable(),
  routes: z.record(discoveryRoute, routePresentation),
}).strict()
export type DiscoveryProject = z.infer<typeof discoveryProject>
export type RoutePresentation = z.infer<typeof routePresentation>

export const routeDefaults = {
  "library-first": { name: "Library-first", title: "从化合物库出发，筛选候选分子", subtitle: "库筛选 · 结构复核 · 分子动力学验证", filter: "重点候选" },
  "ligand-first": { name: "Ligand-first", title: "从已知配体出发，探索共价设计", subtitle: "母体设计 · 结构复核 · 分子动力学验证", filter: "重点候选" },
  "de-novo": { name: "De novo", title: "从口袋出发，发现新分子", subtitle: "口袋定义 · 分子生成 · 结构复核 · 分子动力学验证", filter: "重点候选" },
}

/** Text substitution reads data properties only; it never evaluates code. */
export function renderTemplate<T>(template: T, values: unknown): T {
  const render = (item: unknown): unknown => {
    if (typeof item === "string") return item.replace(/\{\{([\w.]+)\}\}/g, (_, key: string) => {
      const value = key.split(".").reduce<unknown>((current, part) =>
        current && typeof current === "object" && Object.hasOwn(current, part) ? (current as Record<string, unknown>)[part] : undefined, values)
      return typeof value === "number" || typeof value === "string" ? String(value) : "—"
    })
    if (Array.isArray(item)) return item.map(render)
    if (item && typeof item === "object") return Object.fromEntries(Object.entries(item).map(([key, value]) => [key, render(value)]))
    return item
  }
  return render(template) as T
}
