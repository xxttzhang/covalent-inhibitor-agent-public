import z from "zod"

export const workflowObservation = z.object({
  workflowID: z.string(),
  route: z.enum(["ligand-first", "library-first"]),
  sessionID: z.string(),
  title: z.string(),
  active: z.boolean(),
  observed: z
    .object({
      revision: z.number().int().positive(),
      stage: z.string(),
      time: z.number(),
      target: z.string().optional(),
      site: z.string().optional(),
      execution: z.string().optional(),
      task: z.object({ runID: z.string(), state: z.string() }).optional(),
      candidates: z.object({
        generated: z.number().int().nonnegative(),
        admitted: z.number().int().nonnegative(),
        deferred: z.array(z.object({ id: z.string(), reason: z.string() })),
      }).optional(),
      artifacts: z.number().int().nonnegative(),
    })
    .optional(),
  requestFailed: z.boolean(),
})
export const workflowObservations = z.object({
  projectID: z.string(),
  workflows: workflowObservation.array(),
})
export type WorkflowObservation = z.infer<typeof workflowObservation>
