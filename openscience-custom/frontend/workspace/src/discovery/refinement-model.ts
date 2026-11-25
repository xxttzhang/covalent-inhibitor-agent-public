import { z } from "zod"

export const refinement = z.object({
  schema: z.literal("refinement-workbench-v1"), route: z.enum(["library-first", "ligand-first"]),
  project: z.string(), session: z.string(), target: z.string(), collectedAt: z.string().datetime(), evidenceUpdatedAt: z.string().datetime(),
  current: z.string(), notice: z.string(), mdReason: z.string(),
  stats: z.array(z.object({ label: z.string(), value: z.string(), unit: z.string(), note: z.string() })),
  stages: z.array(z.object({ id: z.string(), label: z.string(), state: z.enum(["done", "current", "pending", "blocked"]),
    status: z.string(), count: z.string(), title: z.string(), detail: z.string(), facts: z.array(z.string()) })),
  candidates: z.array(z.object({ row: z.record(z.string(), z.string()), label: z.string(), badge: z.string(),
    tone: z.enum(["teal", "amber", "blue", "gray"]), note: z.string(), featured: z.boolean() })),
  reports: z.array(z.object({ id: z.string(), version: z.string(), title: z.string(), name: z.string(), sha256: z.string() })),
  sources: z.array(z.object({ path: z.string(), sha256: z.string(), updated: z.string() })),
})
export type Refinement = z.infer<typeof refinement>
export type RefinementRoute = Refinement["route"]
