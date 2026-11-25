import { jsonSchema } from "ai"
import z from "zod"
import { Storage } from "../storage/storage"
import { Lock } from "../util/lock"
import type { RecordTools } from "./route-workflow"

const stages = ["vs", "covdock"] as const
const binding = z.object({ entry: z.string().endsWith("_covalent_ligand_workflow"),
  workflow_id: z.string().regex(/^ligand-flow-[a-f0-9]{24}$/), active: z.boolean().optional() }).strict()
const selection = z.object({ stage: z.enum(stages) }).strict()
const record = z.object({ run_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$/),
  request_id: z.string().min(1), job_state: z.enum(["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"]),
  evidence_error: z.unknown().optional(), job_identity_verified: z.boolean().optional(), approval: z.string().optional() })

/** Status is read from the bound server, never a model-supplied task ID. */
export function ligandCancellationTask(output: unknown, workflow: string, stage: typeof stages[number]) {
  const envelope = z.object({ isError: z.boolean().optional(), structuredContent: z.object({
    route: z.literal("ligand-first"), workflow: z.object({ workflow_id: z.literal(workflow) }),
    pending: z.unknown().optional(), vs_execution: z.unknown().optional(), covdock_execution: z.unknown().optional(),
  }) }).parse(output)
  if (envelope.isError || envelope.structuredContent.pending) throw Error("Workflow observation is not settled")
  const task = record.parse(envelope.structuredContent[stage === "vs" ? "vs_execution" : "covdock_execution"])
  if (task.evidence_error || (stage === "vs" ? task.job_identity_verified !== true : task.approval !== "APPROVED"))
    throw Error("Current workflow task identity cannot be verified")
  return task
}

/** Receives native approval/dispatch guarded tools before raw-tool filtering.
 * Keep the original run pinned across model thinking and approval preparation. */
export async function ligandCancellationTools(tools: RecordTools, scope: { project: string; session: string },
  available: () => Promise<boolean>): Promise<RecordTools> {
  const key = ["ligand-workflow-bindings", scope.project, scope.session]
  const saved = await Storage.read(key).catch(error => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  })
  if (!saved) return {}
  const bound = binding.parse(saved)
  if (bound.active === false) return {}
  const prefix = bound.entry.slice(0, -"covalent_ligand_workflow".length)
  const status = tools[bound.entry]?.execute
  if (!status) return {}
  const snapshot = await status({ action: "status", workflow_id: bound.workflow_id },
    { toolCallId: "ligand-cancellation-visibility", messages: [] })
  const tasks = stages.flatMap(stage => {
    try { return [{ stage, task: ligandCancellationTask(snapshot, bound.workflow_id, stage) }] }
    catch { return [] }
  })
  const output: RecordTools = {}
  for (const operation of ["cancel", "observe"] as const) {
    const execute = tools[prefix + (operation === "cancel" ? "covalent_cancel_job" : "covalent_get_job")]?.execute
    const eligible = tasks.filter(item => operation === "observe" || ["QUEUED", "RUNNING"].includes(item.task.job_state))
    if (!execute || !eligible.length) continue
    output[operation === "cancel" ? "ligand_cancel_task" : "ligand_observe_cancellation"] = {
      description: operation === "cancel"
        ? "Request cancellation of this Ligand study's existing VS or CovDock-LO task through independent native approval. Host binds the original run. A request is not confirmed scheduler/vendor termination. Never submits or retries compute."
        : "Observe cancellation of the same bound Ligand task, including persisted stop intent and confirmation. Never sends a new stop or starts compute. Use this after an uncertain cancellation response.",
      inputSchema: jsonSchema({ type: "object", properties: { stage: { type: "string", enum: eligible.map(item => item.stage) } },
        required: ["stage"], additionalProperties: false }),
      execute: async (args, options) => {
        const chosen = selection.parse(args)
        const original = eligible.find(item => item.stage === chosen.stage)
        if (!original) throw Error("This task was not offered for this operation")
        using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
        using __ = await Lock.write(key.join("/"))
        options.abortSignal?.throwIfAborted()
        if (!await available()) throw Error("Research route changed; refresh tools")
        const current = binding.parse(await Storage.read(key))
        if (current.active === false || current.entry !== bound.entry || current.workflow_id !== bound.workflow_id)
          throw Error("Ligand session binding changed; refresh tools")
        const fresh = ligandCancellationTask(await status({ action: "status", workflow_id: bound.workflow_id }, options),
          bound.workflow_id, chosen.stage)
        if (fresh.run_id !== original.task.run_id || fresh.request_id !== original.task.request_id)
          throw Error("Ligand task changed; refusing to operate on a replacement")
        if (operation === "cancel" && !["QUEUED", "RUNNING"].includes(fresh.job_state))
          throw Error("Task is terminal; observe it instead of cancelling")
        options.abortSignal?.throwIfAborted()
        return await execute(operation === "cancel" ? { run_id: fresh.run_id, human_approved: true }
          : { run_id: fresh.run_id }, options)
      },
    }
  }
  return output
}
