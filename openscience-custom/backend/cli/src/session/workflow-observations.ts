import z from "zod"
import { Instance } from "../project/instance"
import { Storage } from "../storage/storage"
import { Session } from "."
import { basename } from "./route-workflow"
import { workflowObservations, type WorkflowObservation } from "@synsci/util/discovery-workflows"

const routes = [
  {
    route: "ligand-first",
    key: "ligand-workflow-bindings",
    protocol: "ligand-workflow-v1",
    pattern: /^ligand-flow-[a-f0-9]{24}$/,
    entry: "covalent_ligand_workflow",
    tools: ["covalent_ligand_workflow", "covalent_ligand_evidence", "covalent_ligand_design_step"],
  },
  {
    route: "library-first",
    key: "library-workflow-bindings",
    protocol: "library-workflow-v1",
    pattern: /^library-flow-[a-f0-9]{24}$/,
    entry: "covalent_library_workflow",
    tools: ["covalent_library_workflow", "covalent_library_research", "covalent_library_receptor"],
  },
] as const
const binding = z.object({ entry: z.string(), workflow_id: z.string(), active: z.boolean().optional() })
const candidates = z.object({
  source_kind: z.literal("sealed-ligand-direct-lo-v1"),
  generated_candidate_count: z.number().int().nonnegative(),
  input_count: z.number().int().nonnegative(),
  selected_candidate_ids: z.array(z.string().min(1).max(200)),
  candidate_deferrals: z.array(z.object({ candidate_id: z.string().min(1).max(200), reason: z.string().min(1).max(200) })),
}).refine(value => value.input_count === value.selected_candidate_ids.length
  && value.generated_candidate_count === value.input_count + value.candidate_deferrals.length
  && new Set([...value.selected_candidate_ids, ...value.candidate_deferrals.map(row => row.candidate_id)]).size === value.generated_candidate_count)
const result = z.object({
  schema_version: z.literal("research-workflow-result-v1"),
  route: z.string(),
  workflow: z.object({
    protocol: z.string(),
    workflow_id: z.string(),
    revision: z.number().int().positive(),
    stage: z.string().min(1).max(100),
  }),
  target: z.object({ target: z.string().max(200).optional(), site: z.string().max(100).optional() }).optional(),
  execution: z.object({ state: z.string().max(100) }).optional(),
  covdock_execution: z.object({
    run_id: z.string().regex(/^[A-Za-z0-9_-]{1,200}$/).optional(),
    job_state: z.string().max(100).optional(),
  }).nullish(),
  artifacts: z.array(z.unknown()).optional(),
  refinement_input: z.unknown().optional(),
})

/** Read only the selected project's bound sessions. These are recorded tool
 * observations, not live executor status and never permission to run a job. */
export async function observedWorkflows() {
  const projectID = Instance.project.id
  const sessions = new Map((await Array.fromAsync(Session.list())).map((session) => [session.id, session]))
  const workflows: WorkflowObservation[] = []
  for (const route of routes) {
    for (const key of await Storage.list([route.key, projectID])) {
      const session = sessions.get(key[2])
      if (!session || key.length !== 3) continue
      const saved = binding.safeParse(await Storage.read(key))
      if (!saved.success || !route.pattern.test(saved.data.workflow_id) || basename(saved.data.entry) !== route.entry)
        continue
      const prefix = saved.data.entry.slice(0, -route.entry.length)
      const messages = await Session.messages({ sessionID: session.id, limit: 200 })
      const observations = messages
        .flatMap((message) => {
          if (message.info.role !== "assistant") return []
          return message.parts.flatMap((part) => {
            if (
              part.type !== "tool" ||
              part.state.status !== "completed" ||
              !route.tools.some((tool) => part.tool === prefix + tool)
            )
              return []
            const value = (() => {
              try {
                return result.safeParse(JSON.parse(part.state.output))
              } catch {
                return undefined
              }
            })()
            if (
              !value?.success ||
              value.data.route !== route.route ||
              value.data.workflow.protocol !== route.protocol ||
              value.data.workflow.workflow_id !== saved.data.workflow_id
            )
              return []
            const batch = candidates.safeParse(value.data.refinement_input)
            return [
              {
                revision: value.data.workflow.revision,
                stage: value.data.workflow.stage,
                time: part.state.time.end,
                target: value.data.target?.target,
                site: value.data.target?.site,
                execution: value.data.execution?.state,
                task: value.data.covdock_execution?.run_id && value.data.covdock_execution?.job_state ? {
                  runID: value.data.covdock_execution.run_id,
                  state: value.data.covdock_execution.job_state,
                } : undefined,
                artifacts: value.data.artifacts?.length ?? 0,
                candidates: batch.success ? {
                  generated: batch.data.generated_candidate_count, admitted: batch.data.input_count,
                  deferred: batch.data.candidate_deferrals.map(row => ({ id: row.candidate_id, reason: row.reason })),
                } : undefined,
              },
            ]
          })
        })
        .sort((a, b) => b.revision - a.revision || b.time - a.time)
      const latest = messages.findLast((message) => message.info.role === "assistant")?.info
      workflows.push({
        workflowID: saved.data.workflow_id,
        route: route.route,
        sessionID: session.id,
        title: session.title,
        active: saved.data.active !== false,
        observed: observations[0],
        requestFailed: latest?.role === "assistant" && !!latest.error,
      })
    }
  }
  return workflowObservations.parse({ projectID, workflows })
}
