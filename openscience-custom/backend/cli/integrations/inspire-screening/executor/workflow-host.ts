import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto"
import z from "zod"
import { covalentSigningKey } from "../../../src/session/covalent-approval"
import { workflowScreeningIntent, prepareWorkflowScreening, recoverWorkflowScreening } from "./workflow-plan"
import { digest, planSchema, resource, states } from "./model"
import { origin, requestHash, type BoundRequest } from "./request"
import { workflowReference, workflowOperation, workflowClosure, readWorkflowClosure, closeWorkflowOperation, workflowObservation } from "./workflow-execution"
import { advanceNativeTask } from "./task-lifecycle"
import { Inspire, createArgs } from "./inspire"
import { settings } from "./host"
import type { Store } from "./store"
import { selection } from "./selection-proposal"
import { screeningResult, selectionBinding, readWorkflowResults } from "./workflow-results"
import { refinementPlanningScope, refinementPlanningClosure, readRefinementPlanningClosure, closeRefinementPlanning } from "./refinement-planning-scope"
import { refinementPlanningResult, prepareWorkflowRefinement } from "./workflow-refinement"
import { advanceNativeRefinement, readRefinementObservation, refinementObservation } from "./refinement-lifecycle"

import { workflowRefinementReport, workflowRefinementResult, readWorkflowRefinementResults, resumeWorkflowRefinementDelivery,
  mdCandidateIdentity, mdCandidateChoice } from "./workflow-refinement-results"
import { RefinementDelivery, refinementDeliveryObservation } from "./refinement-delivery"
import { Publications } from "./publication"
import { mdTransfer, mdInput, mdChunk, mdNativeTarget, readMdNativeTarget, transferMdFiles } from "./workflow-md-transfer"

export const hostScope = z.object({ project: z.string().min(1), directory: z.string().startsWith("/"),
  sessionID: z.string().min(1), ledger_id: z.string().regex(/^[a-f0-9]{32}$/) }).strict()
export const workflowHostActions = ["compile_screening", "recover_screening", "approve_screening", "screening_status",
  "cancel_screening", "wake_screening", "release_screening", "migrate_screening_to_vs", "screening_report", "select_screening_candidates", "read_screening_candidates",
  "plan_refinement", "recover_refinement", "release_refinement", "approve_refinement", "refinement_status", "cancel_refinement", "wake_refinement", "refinement_report", "resume_refinement_delivery", "select_md_candidate", "release_md_candidate", "prepare_md_input"] as const
const argumentsSchema = z.object({ action: z.enum(workflowHostActions),
  workflow_id: workflowReference.shape.workflow_id, expected_revision: z.number().int().positive(),
  reason: z.string().trim().min(5).max(2000).optional(), selection_policy: selection.optional(),
  compound_id: mdCandidateIdentity.shape.compound_id.optional(), input_sdf_index: mdCandidateIdentity.shape.input_sdf_index.optional(),
  refinement_review: z.string().trim().min(20).max(4000).optional(), max_points: z.number().finite().nonnegative().optional() }).strict().superRefine((value, ctx) => {
    if (["release_screening", "migrate_screening_to_vs", "select_screening_candidates", "release_refinement", "select_md_candidate", "release_md_candidate"].includes(value.action) !== (value.reason !== undefined)
      || (value.action === "select_screening_candidates") !== (value.selection_policy !== undefined)
      || value.action === "select_screening_candidates" && (value.reason?.length ?? 0) < 20)
      ctx.addIssue({ code: "custom", message: "Selection requires its explicit policy and review rationale; release requires a reason" })
    if ((value.action === "plan_refinement") !== (value.refinement_review !== undefined)
      || (value.action === "plan_refinement") !== (value.max_points !== undefined))
      ctx.addIssue({ code: "custom", message: "Refinement requires its independent review and proposed budget" })
    if ((value.action === "select_md_candidate") !== (value.compound_id !== undefined)
      || (value.action === "select_md_candidate") !== (value.input_sdf_index !== undefined)
      || value.action === "select_md_candidate" && (value.reason?.length ?? 0) < 20)
      ctx.addIssue({ code: "custom", message: "MD candidate choice requires exact compound/index and a scientific reason" })
  })
const receipt = z.object({ task_id: z.string().regex(/^screen-[a-f0-9]{24}$/), reference: workflowReference,
  state: z.literal("DRAFT"), preflight_verified: z.literal(true) }).strict()
const observation = z.object({ schema: z.literal("library-workflow-ledger-observation-v1"), reference: workflowReference,
  task_id: receipt.shape.task_id, task_state: z.enum(states), approval_recorded: z.boolean(),
  execution_state: z.enum(["not_submitted", "submitted", "running", "completed", "failed", "outcome_unknown"]),
  ledger_sequence: z.number().int().positive(), observed_at: z.number().int().positive(),
  observation_scope: z.literal("persisted_executor_ledger"), assessment: z.literal("not_evaluated"), remote_artifacts_rehashed: z.literal(false),
}).strict()
const response = z.object({ kind: z.literal("library-screening-host-response-v1"),
  request_sha256: z.string(), scope: hostScope,
  screening_plan: z.object({ id: z.string().regex(/^screening-plan-[a-f0-9]{32}$/), sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict(),
  intent: workflowScreeningIntent, source: z.object({ text: z.string().min(1).max(10000), origin }).strict(),
  receipt: receipt.nullable(), observation: observation.nullable().default(null), closure: workflowClosure.nullable().default(null),
  screening_result: screeningResult.nullable().default(null), selection_binding: selectionBinding.nullable().default(null),
  refinement_handoff: z.object({ host_scope: hostScope, source: z.object({ text: z.string().min(1).max(10000), origin }).strict(),
    task_id: receipt.shape.task_id, planning_scope: refinementPlanningScope, selected_count: z.number().int().min(1).max(100),
    receipt: refinementPlanningResult.nullable(), proposal_digest: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
    execution_observation: refinementObservation.nullable().optional(),
    report_result: workflowRefinementResult.optional(), report_witness: workflowRefinementReport.optional(),
    delivery_witness: refinementDeliveryObservation.omit({ checked_at: true }).optional(),
    draft_identity: refinementPlanningResult.shape.draft.unwrap().pick({ workflow_id: true, digest: true, proposal_digest: true, approve_before: true }).nullable(),
  }).strict().nullable().default(null),
  refinement_closure: refinementPlanningClosure.nullable().default(null),
  md_candidate: mdCandidateChoice.nullable().default(null),
  md_transfer: mdTransfer.nullable().default(null), md_input: mdInput.nullable().default(null),
  public_result: z.object({ workflow: z.object({ workflow_id: z.string(),
    revision: z.number().int().positive(), stage: z.string() }).passthrough() }).passthrough(),
}).strict()
type Environment = Omit<Parameters<typeof prepareWorkflowScreening>[3], "source">

export function workflowLedgerIdentity(store: Store) {
  store.db.exec("CREATE TABLE IF NOT EXISTS workflow_host_identity(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)")
  store.db.query("INSERT OR IGNORE INTO workflow_host_identity VALUES(1,?)").run(randomBytes(16).toString("hex"))
  const row = store.db.query("SELECT identity FROM workflow_host_identity WHERE id=1").get() as { identity: string }
  return hostScope.shape.ledger_id.parse(row.identity)
}

/** Authenticate exact encoded bytes; Python/JS never need to agree on float
 * JSON formatting. The signed response is tied to this fresh host challenge.
 * These proofs attest provenance only and cannot authorize a compute tool. */
export async function exchangeWorkflow(input: { execute: (args: Record<string, unknown>) => Promise<unknown>;
  scope: z.infer<typeof hostScope>; args: z.infer<typeof argumentsSchema>; operation: "open" | "commit" | "observe" | "release" | "result" | "refinement_plan" | "refinement_release" | "refinement_observe" | "refinement_report" | "md_candidate_select" | "md_candidate_release" | "md_transfer_open" | "md_transfer_chunk" | "md_transfer_commit";
  source?: BoundRequest; receipt?: z.infer<typeof receipt>; observation?: z.infer<typeof observation>; closure?: z.infer<typeof workflowClosure>;
  screening_result?: z.infer<typeof screeningResult>; refinement_result?: z.infer<typeof refinementPlanningResult>;
  refinement_closure?: z.infer<typeof refinementPlanningClosure>; refinement_observation?: z.infer<typeof refinementObservation>; refinement_report?: z.infer<typeof workflowRefinementResult>;
  md_choice_id?: string; md_chunk?: z.infer<typeof mdChunk>; md_native_target?: z.infer<typeof mdNativeTarget> }) {
  const scope = hostScope.parse(input.scope)
  const args = argumentsSchema.parse(input.args)
  const secret = await covalentSigningKey()
  const data = Buffer.from(JSON.stringify({ kind: "library-screening-host-request-v1", scope, arguments: args,
    operation: input.operation, source: input.source ?? null, receipt: input.receipt ?? null,
    observation: input.observation ?? null, closure: input.closure ?? null, screening_result: input.screening_result ?? null,
    refinement_result: input.refinement_result ?? null,
    refinement_closure: input.refinement_closure ?? null,
    refinement_observation: input.refinement_observation ?? null,
    refinement_report: input.refinement_report ?? null,
    md_choice_id: input.md_choice_id ?? null, md_chunk: input.md_chunk ? mdChunk.parse(input.md_chunk) : null,
    md_native_target: input.md_native_target ? mdNativeTarget.parse(input.md_native_target) : null,
    nonce: randomBytes(16).toString("hex"), expires: Math.floor(Date.now() / 1000) + 300 })).toString("base64url")
  const token = data + "." + createHmac("sha256", secret).update(data).digest("hex")
  const raw = await input.execute({ ...args, _workflow_handoff: token })
  const outer = z.object({ isError: z.boolean().optional(), structuredContent: z.object({ _host_handoff: z.string().max(2_000_000) }).passthrough() }).parse(raw)
  if (outer.isError) throw Error("Backend rejected the workflow planning exchange; recover its existing operation")
  const proof = outer.structuredContent._host_handoff
  const parts = proof.split(".")
  if (parts.length !== 2 || !/^[A-Za-z0-9_-]+$/.test(parts[0]) || !/^[a-f0-9]{64}$/.test(parts[1]))
    throw Error("Invalid workflow response envelope")
  const expected = createHmac("sha256", secret).update(parts[0]).digest()
  if (!timingSafeEqual(expected, Buffer.from(parts[1], "hex"))) throw Error("Invalid workflow backend signature")
  const verified = response.parse(JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8")))
  if (verified.request_sha256 !== createHash("sha256").update(token).digest("hex") || digest(verified.scope) !== digest(scope)
    || verified.intent.workflow_id !== args.workflow_id || verified.public_result.workflow.workflow_id !== args.workflow_id
    || verified.public_result.workflow.revision < args.expected_revision
    || verified.intent.revision > verified.public_result.workflow.revision
    || verified.screening_plan.id !== "screening-plan-" + verified.screening_plan.sha256.slice(0, 32)
    || requestHash(verified.source.text) !== verified.source.origin.request_sha256)
    throw Error("Workflow response scope, challenge, source or plan binding changed")
  if (input.source && digest(args.action === "plan_refinement" ? verified.refinement_handoff?.source : verified.source) !== digest(input.source))
    throw Error("Backend substituted the original request")
  if (verified.refinement_handoff && (digest(verified.refinement_handoff.host_scope) !== digest(scope)
    || requestHash(verified.refinement_handoff.source.text) !== verified.refinement_handoff.source.origin.request_sha256
    || digest(verified.refinement_handoff.planning_scope.parent) !== digest((verified.observation ?? verified.receipt)?.reference)
    || digest(verified.refinement_handoff.planning_scope.selection) !== digest(verified.selection_binding)))
    throw Error("Refinement handoff differs from its host, original request or reviewed parent")
  return verified
}

export async function workflowEnvironment(intent: z.infer<typeof workflowScreeningIntent>): Promise<Environment> {
  const config = await settings()
  const transport = new Inspire(config.connection)
  const assets = z.object({ assets: z.record(z.string(), z.string()), runtime: planSchema.shape.runtime }).passthrough()
    .parse(await transport.remote(config.runtime, ["snapshot", Buffer.from(JSON.stringify(intent.research_inputs)).toString("base64")]))
  if (digest(assets.runtime) !== digest(config.runtime)) throw Error("Remote worker/runtime differs from host configuration")
  const cpu = resource.parse({ ...config.cpu, points_per_hour: 0 })
  const gpu = resource.parse({ ...config.gpu, points_per_hour: 0 })
  const [cpuRate, gpuRate] = await Promise.all([transport.live(cpu), transport.live(gpu)])
  return { assets: assets.assets, runtime: config.runtime, connection: config.connection,
    cpu: { ...cpu, points_per_hour: cpuRate }, gpu: { ...gpu, points_per_hour: gpuRate } }
}

/** One host adapter over the existing compiler, task DB and native lifecycle.
 * Only explicit approval/cancel/wake actions may invoke their original paths. */
export async function runWorkflowScreening(input: { store: Store; execute: (args: Record<string, unknown>) => Promise<unknown>;
  scope: Omit<z.infer<typeof hostScope>, "ledger_id">; args: unknown; source?: BoundRequest; signal: AbortSignal;
  environment?: typeof workflowEnvironment;
  native?: { context: Parameters<typeof advanceNativeTask>[3]; observer?: Parameters<typeof advanceNativeTask>[4];
    deliveryObserver?: Parameters<typeof resumeWorkflowRefinementDelivery>[5]; publications?: Publications } }) {
  input.signal.throwIfAborted()
  const args = argumentsSchema.parse(input.args)
  const scope = hostScope.parse({ ...input.scope, ledger_id: workflowLedgerIdentity(input.store) })
  const verified = await exchangeWorkflow({ ...input, scope, args, operation: "open" })
  input.signal.throwIfAborted()
  const operation = workflowOperation.parse({ schema: "library-workflow-execution-reference-v1",
    workflow_id: verified.intent.workflow_id, revision: verified.intent.revision, operation_id: verified.intent.operation_id,
    intent_sha256: digest(verified.intent) })
  const closed = readWorkflowClosure(input.store, operation, scope)
  if (closed && !["release_screening", "recover_screening", "migrate_screening_to_vs"].includes(args.action))
    throw Error("Workflow operation is closed; recover its release instead of advancing the task")
  const parameters = { ...args, expected_revision: verified.public_result.workflow.revision }
  const present = (value: z.infer<typeof response>) => {
    if (digest(value.intent) !== digest(verified.intent) || digest(value.source) !== digest(verified.source)
      || digest(value.screening_plan) !== digest(verified.screening_plan)) throw Error("Backend receipt changed the committed operation")
    return { content: [{ type: "text" as const, text: JSON.stringify(value.public_result) }],
      structuredContent: value.public_result, isError: false }
  }
  if (closed || args.action === "release_screening" || args.action === "migrate_screening_to_vs") {
    if (closed && args.action !== "recover_screening"
      && (closed.state === "CANCELLED_UNSUBMITTED_FOR_VS") !== (args.action === "migrate_screening_to_vs"))
      throw Error("Workflow closure belongs to another action; recover its original purpose")
    const closure = closed ?? closeWorkflowOperation(input.store, operation, scope, args.reason!,
      args.action === "migrate_screening_to_vs" ? "migrate_to_vs" : "release")
    const released = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters, operation: "release", closure })
    if (digest(released.closure) !== digest(closure)) throw Error("Backend did not acknowledge the exact sealed operation")
    return present(released)
  }
  const recovered = recoverWorkflowScreening(input.store, verified.intent, scope, verified.source)
  if ((verified.receipt || verified.observation) && !recovered) throw Error("Backend has a native task but this host lacks its ledger; do not recreate it")
  const bound = verified.receipt ?? verified.observation
  if (recovered && bound && (bound.task_id !== recovered.task.id || digest(bound.reference) !== digest(recovered.reference)))
    throw Error("Backend and native task identity differ")
  if (["plan_refinement", "recover_refinement", "release_refinement", "approve_refinement", "refinement_status", "cancel_refinement", "wake_refinement", "refinement_report", "resume_refinement_delivery", "select_md_candidate", "release_md_candidate", "prepare_md_input"].includes(args.action)) {
    const refinement = verified.refinement_handoff
    if (!recovered || !refinement || refinement.task_id !== recovered.task.id)
      throw Error("Refinement planning requires the exact completed native task and durable backend operation")
    if (args.action === "plan_refinement" && (refinement.planning_scope.max_points !== args.max_points
      || refinement.planning_scope.scientific_review !== args.refinement_review)) throw Error("Refinement review or proposed budget changed")
    const closed = readRefinementPlanningClosure(input.store, recovered.task.id, refinement.planning_scope)
    if (closed || args.action === "release_refinement") {
      const closure = closed ?? closeRefinementPlanning(input.store, recovered.task, refinement.planning_scope, args.reason!)
      const released = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters,
        operation: "refinement_release", refinement_closure: closure })
      if (released.refinement_handoff || digest(released.refinement_closure) !== digest(closure))
        throw Error("Backend did not acknowledge the exact refinement planning closure")
      return present(released)
    }
    const reference = refinement.draft_identity ? { planning_scope: refinement.planning_scope,
      workflow_id: refinement.draft_identity.workflow_id, digest: refinement.draft_identity.digest } : null
    const observation = reference ? readRefinementObservation(input.store, recovered.task, reference) : null
    if (refinement.execution_observation && refinement.execution_observation.native_state !== "NOT_CREATED" && observation?.native_state === "NOT_CREATED")
      throw Error("Backend knows a native refinement but this ledger lacks it; do not recreate the execution")
    if (args.action === "release_md_candidate") {
      const released = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters, operation: "md_candidate_release" })
      if (released.md_candidate) throw Error("Backend did not release the MD candidate decision")
      return present(released)
    }
    if (args.action === "refinement_report" || args.action === "resume_refinement_delivery" || args.action === "select_md_candidate" || args.action === "prepare_md_input") {
      if (!reference) throw Error("Refinement report requires its exact committed native draft")
      const sync = async (revision = parameters.expected_revision) => {
        const report = await readWorkflowRefinementResults(input.store, recovered.task, reference, scope, input.signal,
          input.native?.publications ? new RefinementDelivery(input.store, input.native.publications) : undefined)
        const committed = await exchangeWorkflow({ ...input, scope, source: undefined, args: { ...parameters, expected_revision: revision },
          operation: "refinement_report", refinement_report: report.result, refinement_observation: report.observation })
        if (digest(committed.refinement_handoff?.report_result) !== digest(report.result)
          || digest(committed.refinement_handoff?.execution_observation) !== digest(report.observation))
          throw Error("Backend did not acknowledge the exact refinement report observation")
        return committed
      }
      if (args.action === "resume_refinement_delivery") {
        try {
          await resumeWorkflowRefinementDelivery(input.store, recovered.task, reference, scope, input.signal, input.native?.deliveryObserver)
        } catch (error) {
          await sync().catch(() => input.store.event(recovered.task.id, "refinement_delivery_receipt_recovery_required",
            { workflow_id: reference.workflow_id, operation_id: refinement.planning_scope.operation_id }))
          throw error
        }
      }
      const refreshed = await sync()
      if (args.action === "select_md_candidate") {
        const chosen = await exchangeWorkflow({ ...input, scope, source: undefined,
          args: { ...parameters, expected_revision: refreshed.public_result.workflow.revision },
          operation: "md_candidate_select", refinement_report: refreshed.refinement_handoff?.report_result })
        const choice = chosen.md_candidate
        if (!choice || choice.candidate.compound_id !== args.compound_id || choice.candidate.input_sdf_index !== args.input_sdf_index
          || choice.reason !== args.reason || choice.native_workflow_id !== reference.workflow_id || choice.native_workflow_digest !== reference.digest
          || choice.task_id !== recovered.task.id || choice.report_digest !== refreshed.refinement_handoff?.report_result?.report?.report_digest)
          throw Error("Backend did not bind the exact MD candidate decision")
        return present(chosen)
      }
      if (args.action === "prepare_md_input") {
        const choice = refreshed.md_candidate
        if (!choice || choice.native_workflow_id !== reference.workflow_id || choice.native_workflow_digest !== reference.digest
          || choice.task_id !== recovered.task.id || choice.report_digest !== refreshed.refinement_handoff?.report_result?.report?.report_digest)
          throw Error("MD preparation requires the exact freshly verified candidate decision")
        const target = readMdNativeTarget(input.store, recovered.task, reference, choice)
        const cursor = { value: await exchangeWorkflow({ ...input, scope, source: undefined,
          args: { ...parameters, expected_revision: refreshed.public_result.workflow.revision },
          operation: "md_transfer_open", md_choice_id: choice.id, md_native_target: target }) }
        if (!cursor.value.md_transfer || digest(cursor.value.md_candidate) !== digest(choice)
          || digest(cursor.value.md_transfer.native_target_context) !== digest(target))
          throw Error("Backend did not open the selected MD transfer")
        await transferMdFiles(choice, cursor.value.md_transfer, input.native?.publications ?? new Publications(input.store), recovered.task, input.signal, async chunk => {
          input.signal.throwIfAborted()
          const next = await exchangeWorkflow({ ...input, scope, source: undefined,
            args: { ...parameters, expected_revision: cursor.value.public_result.workflow.revision },
            operation: "md_transfer_chunk", md_chunk: chunk })
          if (digest(next.md_candidate) !== digest(choice) || next.md_transfer?.choice_id !== choice.id
            || next.md_transfer.offsets[chunk.file] !== chunk.offset + Buffer.from(chunk.data, "base64").byteLength)
            throw Error("Backend did not acknowledge the exact MD byte offset")
          cursor.value = next
        })
        // Revalidate current publication versions after transferring all bytes.
        // Commit an observed failure too, so stale evidence cannot freeze inputs.
        cursor.value = await sync(cursor.value.public_result.workflow.revision)
        input.signal.throwIfAborted()
        if (digest(readMdNativeTarget(input.store, recovered.task, reference, choice)) !== digest(target))
          throw Error("Native target context changed during MD transfer")
        const frozen = await exchangeWorkflow({ ...input, scope, source: undefined,
          args: { ...parameters, expected_revision: cursor.value.public_result.workflow.revision },
          operation: "md_transfer_commit", md_choice_id: choice.id, md_native_target: target,
          refinement_report: cursor.value.refinement_handoff?.report_result })
        if (digest(frozen.md_candidate) !== digest(choice) || frozen.md_input?.choice_id !== choice.id
          || frozen.md_transfer?.state !== "frozen") throw Error("Backend did not freeze the selected MD input")
        return present(frozen)
      }
      return present(refreshed)
    }
    const lifecycle = { approve_refinement: "approve", cancel_refinement: "cancel", wake_refinement: "wake", refinement_status: "status" } as const
    const action = lifecycle[args.action as keyof typeof lifecycle]
    if (action || args.action === "recover_refinement" && observation && observation.native_state !== "NOT_CREATED") {
      if (!reference) throw Error("Refinement lifecycle requires its exact committed native draft")
      const sync = async () => {
        const value = readRefinementObservation(input.store, input.store.get(recovered.task.id), reference)
        const result = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters,
          operation: "refinement_observe", refinement_observation: value })
        if (digest(result.refinement_handoff?.execution_observation) !== digest(value))
          throw Error("Backend did not acknowledge the exact refinement ledger observation")
        return present(result)
      }
      if (action && action !== "status") {
        const native = input.native
        if (!native || native.context.sessionID !== scope.sessionID || native.context.directory !== scope.directory)
          throw Error("Refinement lifecycle requires the bound native permission context")
        try { await advanceNativeRefinement(input.store, recovered.task, action, native.context, reference, native.observer) }
        catch (error) {
          await sync().catch(() => input.store.event(recovered.task.id, "refinement_workflow_receipt_recovery_required",
            { operation_id: refinement.planning_scope.operation_id }))
          throw error
        }
      }
      return sync()
    }
    const result = await prepareWorkflowRefinement(input.store, recovered.task, refinement.planning_scope, refinement.source, input.signal)
    const committed = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters,
      operation: "refinement_plan", refinement_result: result })
    if (digest(committed.refinement_handoff?.planning_scope) !== digest(refinement.planning_scope)
      || digest(committed.refinement_handoff?.source) !== digest(refinement.source)
      || digest(committed.refinement_handoff?.receipt) !== digest(result)) throw Error("Backend did not acknowledge the exact refinement operation")
    return present(committed)
  }
  if (["screening_report", "select_screening_candidates", "read_screening_candidates"].includes(args.action)) {
    if (!recovered) throw Error("Result review requires the bound native task")
    if (args.action === "read_screening_candidates" && !verified.selection_binding) throw Error("No subset is bound to this workflow")
    const result = await readWorkflowResults(input.store, recovered.reference, scope, {
      bound: verified.selection_binding, selection: args.action === "select_screening_candidates"
        ? { policy: args.selection_policy!, reason: args.reason! } : undefined,
    }, input.signal)
    const value = observation.parse(workflowObservation(input.store, recovered.reference, scope))
    const committed = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters,
      operation: "result", screening_result: result, observation: value })
    if (digest(committed.screening_result) !== digest(result) || digest(committed.observation) !== digest(value))
      throw Error("Backend did not acknowledge the exact result observation")
    return present(committed)
  }
  const actions = { approve_screening: "approve", cancel_screening: "cancel", wake_screening: "wake", screening_status: "status" } as const
  const action = actions[args.action as keyof typeof actions]
  if (action || recovered && recovered.task.state !== "DRAFT") {
    if (!recovered) throw Error("No native task exists; recover compilation or release the unapproved operation")
    const sync = async () => {
      const value = observation.parse(workflowObservation(input.store, recovered.reference, scope))
      const result = await exchangeWorkflow({ ...input, scope, source: undefined, args: parameters, operation: "observe", observation: value })
      if (digest(result.observation) !== digest(value)) throw Error("Backend did not acknowledge the exact ledger observation")
      return present(result)
    }
    if (action && action !== "status") {
      const native = input.native
      if (!native || native.context.sessionID !== scope.sessionID || native.context.directory !== scope.directory)
        throw Error("Native lifecycle requires the bound host permission context")
      try { await advanceNativeTask(input.store, recovered.task.id, action, native.context, native.observer) }
      catch (error) {
        // Preserve native rejection/abort semantics, but persist cancellation
        // or a successfully recorded approval even if observer startup failed.
        await sync().catch(() => input.store.event(recovered.task.id, "workflow_receipt_recovery_required", { operation_id: operation.operation_id }))
        throw error
      }
    }
    return sync()
  }
  const saved = recovered ?? prepareWorkflowScreening(input.store, verified.intent, scope,
    { ...await (input.environment ?? workflowEnvironment)(verified.intent), source: verified.source })
  const planned = receipt.parse({ task_id: saved.task.id, reference: saved.reference, state: saved.task.state,
    preflight_verified: true })
  if (saved.task.approval || verified.receipt && digest(verified.receipt) !== digest(planned))
    throw Error("Existing workflow task requires lifecycle reconciliation; no replacement or reapproval")
  input.signal.throwIfAborted()
  const records = input.store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='workflow_plan_preflight_verified'")
    .all(saved.task.id) as { payload: string }[]
  if (!records.some(item => digest(JSON.parse(item.payload)) === digest({ plan_sha256: saved.task.digest }))) {
    const transport = new Inspire(saved.task.plan.connection)
    for (const stage of input.store.stages(saved.task.id)) {
      input.signal.throwIfAborted()
      await transport.call(createArgs(saved.task.plan, stage, true))
    }
    input.store.event(saved.task.id, "workflow_plan_preflight_verified", { plan_sha256: saved.task.digest })
  }
  input.signal.throwIfAborted()
  const committed = await exchangeWorkflow({ ...input, scope, source: undefined, operation: "commit", receipt: planned,
    args: { ...args, expected_revision: verified.public_result.workflow.revision } })
  if (digest(committed.receipt) !== digest(planned))
    throw Error("Backend receipt changed the committed operation")
  // Return only the authenticated public projection. Neither encoded proofs nor
  // absolute input roles enter model output or ordinary tool trace storage.
  return present(committed)
}

// Retain the internal preparation name for callers/tests from 1.27.4.
export const compileWorkflowDraft = runWorkflowScreening
