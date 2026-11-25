import { z } from "zod"
import { projectPath } from "@synsci/util/discovery-config"
export const binding = z.object({
  schema: z.literal("discovery-runtime-v1"), project: z.string(), target: z.string(), sourceRoot: z.string(),
  profile: z.literal("audited-opls-covdock-v1"),
  study: z.object({ project: z.string(), session: z.string(), task: z.string() }),
  values: z.object({ denovo: z.record(z.string(), z.string()), refinement: z.record(z.string(), z.string()) }),
  libraryMD: z.object({ directory: projectPath, pins: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)), notes: z.record(z.string(), z.string()) }).optional(),
  ligandMD: z.object({ source: projectPath, sha256: z.string().regex(/^[a-f0-9]{64}$/), dockingSource: projectPath, dockingSha256: z.string().regex(/^[a-f0-9]{64}$/),
    chartsSource: projectPath.optional(), chartsSha256: z.string().regex(/^[a-f0-9]{64}$/).optional(), project: z.string(), run: z.string(), inspireProject: z.string(), candidates: z.record(z.string(), z.string())
  }).refine(value => !!value.chartsSource === !!value.chartsSha256, "Ligand MD chart source and hash must be paired").optional(),
  md: z.object({ source: projectPath, python: z.string(), scripts: z.array(projectPath), expected: z.number().int().positive(), chartCount: z.number().int().positive(), outputPrefix: projectPath, statePath: projectPath }),
}).strict()
export type Binding = z.infer<typeof binding>
