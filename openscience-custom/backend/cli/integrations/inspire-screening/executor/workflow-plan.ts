import z from "zod"
import { compileScreening } from "./compile"
import { conditions } from "./intake"
import { digest, researchInputs } from "./model"
import { origin, requestHash, type BoundRequest } from "./request"
import type { Store } from "./store"
import { prepareWorkflowExecution, recoverWorkflowOperation, workflowOperation } from "./workflow-execution"

/** Internal preparation contract. Backend provenance must be verified by the
 * host before calling this adapter; parsing JSON is not that verification.
 * Deliberately absent from model-facing MCP schemas until the backend handoff
 * and host provenance check are connected. No permission is carried here. */
export const workflowScreeningIntent = z.object({
  schema: z.literal("library-workflow-screening-intent-v1"),
  workflow_id: workflowOperation.shape.workflow_id,
  revision: workflowOperation.shape.revision,
  operation_id: workflowOperation.shape.operation_id,
  conditions: conditions.extend({ screening_mode: z.literal("research_pilot_v1"),
    requested_mode: z.literal("research_pilot_v1").optional(),
    max_points: z.number().finite().nonnegative() }).strict(),
  research_inputs: researchInputs,
  screening_inputs: z.object({
    artifact_id: z.string().regex(/^screening-inputs-[a-f0-9]{32}$/),
    manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  }).strict(),
  scientific_review: z.object({
    construct_and_components: z.string().trim().min(20).max(4000),
    reaction_scope: z.string().trim().min(20).max(4000),
    prefix_scope: z.string().trim().min(20).max(4000),
  }).strict(),
  receptor_evidence: z.object({
    artifact_id: z.string().regex(/^screening-receptor-[a-f0-9]{32}$/),
    manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    receptor_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  }).strict(),
}).strict().superRefine((value, ctx) => {
  if (value.receptor_evidence.artifact_id !== "screening-receptor-" + value.receptor_evidence.manifest_sha256.slice(0, 32))
    ctx.addIssue({ code: "custom", message: "Receptor evidence content identity changed" })
  if (value.research_inputs.receptor.sha256 !== value.receptor_evidence.receptor_sha256)
    ctx.addIssue({ code: "custom", message: "Screening receptor differs from the workflow evidence" })
})
export type WorkflowScreeningIntent = z.infer<typeof workflowScreeningIntent>
type Scope = { directory: string; sessionID: string }
type Environment = Omit<Parameters<typeof compileScreening>[0], "intent" | "source" | "directory" | "session" | "research_inputs"> & {
  source: BoundRequest
}

function operation(intent: WorkflowScreeningIntent) {
  return workflowOperation.parse({ schema: "library-workflow-execution-reference-v1",
    workflow_id: intent.workflow_id, revision: intent.revision, operation_id: intent.operation_id,
    intent_sha256: digest(intent) })
}

function request(source: BoundRequest) {
  const parsed = origin.parse(source.origin)
  if (requestHash(source.text) !== parsed.request_sha256) throw Error("Workflow source differs from the host-bound user message")
  // A restarted assistant may repeat the same operation. The user message and
  // exact real text parts must remain the same; keep the first assistant ID in
  // the frozen plan rather than modifying it on recovery.
  return { text: source.text, user_message_id: parsed.user_message_id, part_ids: parsed.part_ids,
    request_sha256: parsed.request_sha256 }
}

/** Call before reading new live prices or assets. A committed plan is returned
 * unchanged, including cancelled/expired plans; recovery is never reapproval. */
export function recoverWorkflowScreening(store: Store, value: WorkflowScreeningIntent, scope: Scope, source: BoundRequest) {
  const intent = workflowScreeningIntent.parse(value)
  const bound = request(source)
  const saved = recoverWorkflowOperation(store, operation(intent), scope)
  if (!saved) return null
  const records = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='workflow_screening_intent_bound'")
    .all(saved.task.id) as { payload: string }[]
  if (records.length !== 1 || digest(JSON.parse(records[0].payload)) !== digest({ intent, intent_sha256: digest(intent) }))
    throw Error("Workflow task lacks its exact committed scientific intent; reconcile instead of adopting it")
  if (!saved.task.plan.origin || digest(request({ text: saved.task.plan.request, origin: saved.task.plan.origin })) !== digest(bound))
    throw Error("Workflow operation belongs to another original user request")
  // The persisted scientific intent and native plan remain linked even when a
  // later deployment changes the compiler. Never recompile historical tasks.
  const plan = saved.task.plan
  if (digest(plan.research_inputs) !== digest(intent.research_inputs) ||
    plan.automation?.intent_digest !== digest(intent.conditions) ||
    plan.automation.requested_mode !== "research_pilot_v1" || plan.automation.selected_mode !== "research_pilot_v1")
    throw Error("Workflow scientific intent differs from its frozen execution plan")
  return { ...saved, created: false as const, compute_submitted_by_adapter: false as const }
}

/** Compile through the existing compiler and commit the one native DRAFT in
 * the existing task database. No alternate job store, native approval, daemon,
 * or submission path is introduced. Caller supplies the worker-verified roles
 * and live environment; this function does not certify remote freshness. */
export function prepareWorkflowScreening(store: Store, value: WorkflowScreeningIntent, scope: Scope, environment: Environment) {
  const intent = workflowScreeningIntent.parse(value)
  request(environment.source)
  return store.db.transaction(() => {
    // This read and the nested existing task/binding commit share the write
    // transaction. Competing host processes cannot compile two adoptable plans.
    const prior = recoverWorkflowScreening(store, intent, scope, environment.source)
    if (prior) return prior
    const plan = compileScreening({ ...environment, directory: scope.directory, session: scope.sessionID,
      intent: intent.conditions, research_inputs: intent.research_inputs })
    const reference = { ...operation(intent), plan_sha256: digest(plan) }
    const result = prepareWorkflowExecution(store, reference, plan, scope)
    store.event(plan.id, "workflow_screening_intent_bound", { intent, intent_sha256: reference.intent_sha256 })
    return { ...result, reference }
  }).immediate()
}
