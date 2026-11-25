import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, readFile, realpath } from "node:fs/promises"
import type { ToolContext } from "@synsci/plugin/tool"
import { Session } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { planSchema, resource } from "../inspire-screening/executor/model"
import { requestHash, type BoundRequest } from "../inspire-screening/executor/request"
import type { Store } from "../inspire-screening/executor/store"
import { researchContract, researchDigest, researchHash } from "./research-loop-contract"
import { ResearchLoopStore } from "./research-loop-store"
import { compileResearchExecution, preparedResearchExecution, researchExecutionBinding } from "./research-execution"
import { ResearchModelBudgetPolicy } from "./research-model-budget"
import { recoverResearchLiveDecision, runResearchLiveDecision } from "./research-live"
import { researchSnapshot } from "./research-loop"
import { registerResearchNativePlan, readResearchNativeAction, advanceResearchNativeAction } from "./research-native-controller"
import { observeResearchNativeAction } from "./research-native-observation"
import { prepareResearchPriorUsage } from "./research-prior-usage"

const text = z.string().min(1)
const file = z.object({ path: z.string().startsWith("/"), sha256: researchHash }).strict()
export const researchHostSpec = z.object({
  id: z.string().regex(/^[a-z0-9][a-z0-9-]{0,79}$/), label: text, workspace_project: text,
  contract: researchContract,
  request: z.object({ session_id: text, user_message_id: text, sha256: researchHash }).strict(),
  execution: z.object({ connection: planSchema.shape.connection, runtime: planSchema.shape.runtime,
    assets: planSchema.shape.assets, cpu: resource, gpu: resource, research_inputs: planSchema.shape.research_inputs }).strict(),
  binding: researchExecutionBinding,
  policy: ResearchModelBudgetPolicy, policy_verification: file, policy_evidence: z.array(file.extend({ ref: text })).min(1),
  limits: z.object({ max_output_tokens: z.number().int().positive(), timeout_ms: z.number().int().positive().max(2_147_483_647) }).strict(),
  valid_until: z.string().datetime(),
}).strict().superRefine((value, ctx) => {
  if (value.contract.mode !== "native-compute" || value.contract.session !== value.request.session_id
    || value.contract.allowed_adaptations.includes("next_frozen_batch"))
    ctx.addIssue({ code: "custom", message: "Host specifications require one request-bound native pilot; no second-batch capability is registered" })
  if ((value.policy.pricing.basis === "user-authorized-unbounded") !== (value.contract.limits.resources.model_cost === null))
    ctx.addIssue({ code: "custom", message: "Unbounded model pricing requires an explicitly uncapped contract" })
  if (value.limits.max_output_tokens > value.policy.output_token_cap
    || value.policy.input_token_cap + value.limits.max_output_tokens > value.contract.limits.resources.model_tokens)
    ctx.addIssue({ code: "custom", message: "Contract cannot reserve one complete bounded model request" })
})
export type ResearchHostSpec = z.infer<typeof researchHostSpec>
export const researchHostCatalog = z.object({ schema: z.literal("drug-design-research-host-catalog-v1"),
  specs: z.array(researchHostSpec) }).strict().superRefine((value, ctx) => {
  if (new Set(value.specs.map(item => item.id)).size !== value.specs.length)
    ctx.addIssue({ code: "custom", message: "Host specification IDs must be unique" })
})
export type ResearchHostCatalog = z.infer<typeof researchHostCatalog>
export const researchHostAction = z.object({ action: z.enum(["list", "open", "read", "tick", "recover"]),
  spec_id: text.optional(), loop_id: text.optional() }).strict().superRefine((value, ctx) => {
  if (value.action === "open" ? !value.spec_id || value.loop_id !== undefined
    : value.action === "list" ? value.spec_id !== undefined || value.loop_id !== undefined : !value.loop_id || value.spec_id !== undefined)
    ctx.addIssue({ code: "custom", message: "open needs only spec_id; read/tick/recover need only loop_id; list needs neither" })
})
type Context = ToolContext & { callID?: string }
type StepContext = { sessionID: string; messageID: string; callID: string; abort: AbortSignal; tool?: Context }
type EvaluationIdentity = { research: ResearchLoopStore; native: Store; id: string; decision_id?: string; abort?: AbortSignal }
/** Only the installed host supplies this fixed adapter; it is never a tool
 * argument, path or model-authored command. Recovery cannot start chemistry. */
export type ResearchHostEvaluation = {
  evaluate(input: EvaluationIdentity): Promise<{ status: string; evaluation_ref?: string | null; reason?: string; loop?: unknown }>
  recover(input: EvaluationIdentity): Promise<{ status: string; evaluation_ref?: string | null; reason?: string; loop?: unknown }>
}
const reviewSchema = z.object({ schema: z.literal("research-host-policy-verification-v1"), policy_digest: researchHash,
  status: z.literal("verified"), evidence_refs: z.array(text).min(1), reviewed_at: z.string().datetime({ offset: true }), review_basis: text }).strict()
const sha = (bytes: string | Uint8Array) => createHash("sha256").update(bytes).digest("hex")

async function boundFile(input: z.infer<typeof file>) {
  const filename = path.resolve(input.path)
  if (await realpath(filename) !== filename) throw Error("Host evidence path was redirected")
  const info = await lstat(filename)
  if (!info.isFile() || info.size > 32 * 1024 * 1024) throw Error("Host evidence must be a bounded regular file")
  const bytes = await readFile(filename)
  if (sha(bytes) !== input.sha256) throw Error("Host evidence hash changed")
  return JSON.parse(bytes.toString("utf8")) as unknown
}

/** An installation/supervisor hash is the trust anchor. A project file saying
 * policy_verified=true is not an authorization. No tool argument controls this
 * filename or expected hash. Loading performs no model/job/service operation. */
export async function loadResearchHostCatalog(filename: string, expected: string | undefined) {
  if (!expected) return null
  return researchHostCatalog.parse(await boundFile({ path: filename, sha256: researchHash.parse(expected) }))
}

async function caller(context: Context, directory: string) {
  context.abort.throwIfAborted()
  if (!context.callID || context.directory !== directory || await realpath(directory) !== directory)
    throw Error("Research actions require the actual platform tool call and project context")
  const session = await Session.get(context.sessionID)
  const message = await MessageV2.get({ sessionID: context.sessionID, messageID: context.messageID })
  if (session.directory !== directory || message.info.role !== "assistant" || message.info.id !== context.messageID
    || message.info.sessionID !== context.sessionID || message.info.agent !== context.agent)
    throw Error("Research tool belongs to another platform conversation")
  const calls = message.parts.filter(part => part.type === "tool" && part.callID === context.callID)
  if (calls.length !== 1 || calls[0]!.type !== "tool" || calls[0]!.tool !== "discovery_research"
    || !["pending", "running"].includes(calls[0]!.state.status)) throw Error("Research call is not the platform's pending tool invocation")
  const original = await MessageV2.get({ sessionID: context.sessionID, messageID: message.info.parentID })
  const user = MessageV2.User.parse(original.info)
  const parts = original.parts.filter(part => part.type === "text" && !part.synthetic && !part.ignored)
  const request = parts.map(part => part.type === "text" ? part.text : "").join("\n")
  if (!request.trim() || request.length > 10000 || !parts.length || parts.some(part => part.sessionID !== context.sessionID || part.messageID !== user.id))
    throw Error("Research requires the actual nonsynthetic parent user request")
  const source: BoundRequest = { text: request, origin: { user_message_id: user.id, assistant_message_id: context.messageID,
    part_ids: parts.map(part => part.id), request_sha256: requestHash(request) } }
  return { user, source, call_id: context.callID, part_digest: researchDigest(parts), user_digest: researchDigest(user) }
}

type Binding = { loop: string; session: string; spec_id: string; spec: string; spec_digest: string; catalog_digest: string;
  source: string; user_digest: string; part_digest: string; prepared: string; prepared_digest: string }
type Slot = { loop: string; ordinal: number; key: string; message: string; call: string }

/** Stateful project-scoped host. The stores, frozen catalog and permission
 * functions are installation inputs, not remotely/model-callable arguments. */
export function createResearchHost(input: { directory: string; project: string; research: ResearchLoopStore; native: Store;
  catalog: ResearchHostCatalog | null; catalog_digest: string | null; authorizeRemote: () => Promise<void>;
  authorizeEvaluation: () => Promise<void>; evaluation?: ResearchHostEvaluation }) {
  const catalog = input.catalog ? researchHostCatalog.parse(structuredClone(input.catalog)) : null
  if (catalog) researchHash.parse(input.catalog_digest)
  const store = input.research
  store.db.exec(`CREATE TABLE IF NOT EXISTS research_host_bindings(loop TEXT PRIMARY KEY,session TEXT NOT NULL,
    spec_id TEXT NOT NULL,spec TEXT NOT NULL,spec_digest TEXT NOT NULL,catalog_digest TEXT NOT NULL,source TEXT NOT NULL,
    user_digest TEXT NOT NULL,part_digest TEXT NOT NULL,prepared TEXT NOT NULL,prepared_digest TEXT NOT NULL,
    UNIQUE(session));
    CREATE TABLE IF NOT EXISTS research_host_model_slots(loop TEXT NOT NULL,ordinal INTEGER NOT NULL,key TEXT NOT NULL,
    message TEXT NOT NULL,call TEXT NOT NULL,PRIMARY KEY(loop,ordinal),UNIQUE(loop,key));`)
  const attached = new Set<string>()
  const bound = (id: string, context: { sessionID: string }) => {
    const row = store.db.query("SELECT * FROM research_host_bindings WHERE loop=?").get(id) as Binding | null
    if (!row || row.session !== context.sessionID) throw Error("Research loop is not bound to this conversation")
    const spec = researchHostSpec.parse(JSON.parse(row.spec)), loop = store.get(id)
    const prepared = preparedResearchExecution.parse(JSON.parse(row.prepared))
    if (spec.contract.directory !== input.directory || spec.workspace_project !== input.project || spec.contract.session !== row.session
      || row.spec_digest !== researchDigest(spec) || row.prepared_digest !== researchDigest(prepared)
      || loop.digest !== prepared.contract_digest || loop.digest !== researchDigest(spec.contract)
      || row.spec_id !== spec.id || (catalog && (row.catalog_digest !== input.catalog_digest
        || researchDigest(catalog.specs.find(item => item.id === row.spec_id)) !== row.spec_digest)))
      throw Error("Frozen research host binding changed")
    const source = JSON.parse(row.source) as BoundRequest
    const registration = store.evidence(id, "host").find(item => item.key === "host-registration")?.payload as { request?: unknown; tool_call_id?: string } | undefined
    if (!registration || !registration.tool_call_id || researchDigest(source.origin) !== researchDigest(registration.request)
      || source.origin.request_sha256 !== requestHash(source.text) || source.origin.user_message_id !== spec.request.user_message_id
      || source.origin.request_sha256 !== spec.request.sha256 || researchDigest(source.origin) !== researchDigest(prepared.plan.origin))
      throw Error("Original research request binding changed")
    return { row, spec, loop, prepared, source, call_id: registration.tool_call_id }
  }
  const view = (id: string, context: { sessionID: string }) => {
    const value = bound(id, context), snapshot = researchSnapshot(store, id)
    const selected = store.evidence(id, "host").some(item => item.key.startsWith("native-selection:"))
    const native = selected ? readResearchNativeAction({ research: store, native: input.native, id }) : null
    return { loop_id: id, spec_id: value.spec.id, label: value.spec.label, state: snapshot.loop.state, outcome: snapshot.loop.outcome,
      target: `${snapshot.loop.contract.target.gene} ${snapshot.loop.contract.target.canonical_site}`,
      scope: { records: snapshot.loop.contract.limits.max_library_records, candidates_per_round: snapshot.loop.contract.limits.max_candidates_per_round,
        max_rounds: snapshot.loop.contract.limits.max_rounds, second_batch_available: false },
      budget: snapshot.loop.ledger, native: native ? { task_id: native.task_id, status: native.status, decision_id: native.decision_id,
        cancellation: native.cancellation, actual_usage: native.compute_usage } : null,
      observations: snapshot.observations.map(item => ({ id: item.id, task_id: item.run.task_id, classification: item.classification,
        counts: item.counts, missing_evidence: item.missing_evidence })),
      decisions: store.evidence(id, "decision").map(item => ({ id: item.key, content: item.payload })),
      unresolved_model_slots: slots(id).filter(slot => !consumed(id, slot.key)).map(slot => slot.key),
      source: { user_message_id: value.source.origin.user_message_id, request_sha256: value.source.origin.request_sha256 },
      note: "计算批准仅来自平台原生确认；未知用量仍占用预算。执行成功、文件交付和科学通过分别记录。" }
  }
  const offered = (id: string) => store.evidence(id, "host").some(item => item.key === "native-plan:round-1")
  const original = async (value: ReturnType<typeof bound>) => {
    const message = await MessageV2.get({ sessionID: value.row.session, messageID: value.source.origin.user_message_id })
    const user = MessageV2.User.parse(message.info)
    const parts = message.parts.filter(part => part.type === "text" && !part.synthetic && !part.ignored)
    if (researchDigest(user) !== value.row.user_digest || researchDigest(parts) !== value.row.part_digest
      || researchDigest(parts.map(part => part.id)) !== researchDigest(value.source.origin.part_ids))
      throw Error("Original research user message changed")
    return user
  }
  const policy = async (spec: ResearchHostSpec) => {
    const review = reviewSchema.parse(await boundFile(spec.policy_verification))
    const sources = await Promise.all(spec.policy_evidence.map(boundFile))
    const refs = spec.policy_evidence.map(item => item.ref)
    if (new Set(refs).size !== refs.length || refs.some(ref => /^(?:host-|native-|model-)/.test(ref))
      || researchDigest([...refs].sort()) !== researchDigest([...spec.policy.evidence_refs].sort())
      || researchDigest([...refs].sort()) !== researchDigest([...review.evidence_refs].sort())
      || review.policy_digest !== researchDigest(spec.policy) || Date.parse(review.reviewed_at) > Date.now())
      throw Error("Reviewed endpoint/price evidence does not match the frozen research policy")
    return { review, sources }
  }
  const slots = (id: string) => store.db.query("SELECT * FROM research_host_model_slots WHERE loop=? ORDER BY ordinal").all(id) as Slot[]
  const consumed = (id: string, key: string) => store.evidence(id, "decision").some(item => {
    const record = item.payload as { provenance?: { kind?: string; receipt_ref?: string } }
    return record.provenance?.kind === "model_response" && record.provenance.receipt_ref === `model-receipt:${key}`
  })

  return { async execute(raw: unknown, context: Context) {
    const args = researchHostAction.parse(raw), call = await caller(context, input.directory)
    if (args.action === "list") return { configured: Boolean(catalog),
      specifications: (catalog?.specs ?? []).filter(spec => spec.workspace_project === input.project
        && spec.contract.directory === input.directory && spec.request.session_id === context.sessionID)
        .map(spec => ({ id: spec.id, label: spec.label, target: `${spec.contract.target.gene} ${spec.contract.target.canonical_site}`,
          limits: spec.contract.limits, request_message_id: spec.request.user_message_id })),
      note: catalog ? "已登记研究方案；open冻结状态并计入已有科学准备消耗，不启动新计算。" : "尚未安装经宿主校验的研究方案；此工具不能自行批准模型价格或计算方案。" }
    if (args.action === "open") {
      if (!catalog) throw Error("No installation-verified research catalog is configured")
      const existing = store.db.query("SELECT loop,spec_id FROM research_host_bindings WHERE session=?").get(context.sessionID) as Pick<Binding, "loop" | "spec_id"> | null
      if (existing) {
        if (existing.spec_id !== args.spec_id) throw Error("This conversation already has an immutable research scope; use its existing loop")
        const value = bound(existing.loop, context)
        const priorUsage = await prepareResearchPriorUsage(value.spec.contract)
        priorUsage?.verify(store, existing.loop)
        if (!offered(existing.loop)) await registerResearchNativePlan({ research: store, id: existing.loop, key: "round-1", round: 1, prepared: value.prepared })
        return { step: "opened", resumed: true, ...view(existing.loop, context) }
      }
      const spec = catalog.specs.find(spec => spec.id === args.spec_id)
      if (!spec || spec.workspace_project !== input.project || spec.contract.directory !== input.directory
        || spec.request.session_id !== context.sessionID || spec.request.user_message_id !== call.user.id
        || spec.request.sha256 !== call.source.origin.request_sha256 || Date.parse(spec.valid_until) <= Date.now())
        throw Error("Registered research scope does not match this actual user request, project or validity window")
      if (call.user.model.providerID !== spec.policy.provider_id || call.user.model.modelID !== spec.policy.model_id
        || call.user.tier !== undefined || call.user.variant !== undefined) throw Error("Actual user-selected model differs from the reviewed research endpoint policy")
      const proof = await policy(spec)
      const prepared = await compileResearchExecution(spec.contract, { ...spec.execution, source: call.source }, spec.binding,
        { finish_before: Math.min(Date.now() + spec.contract.limits.max_elapsed_ms, Date.parse(spec.valid_until)) })
      const priorUsage = await prepareResearchPriorUsage(spec.contract)
      if (prepared.resources.cpu_core_hours + (priorUsage?.receipt.resources.cpu_core_hours ?? 0)
        > spec.contract.limits.resources.cpu_core_hours + 1e-9)
        throw Error("Cumulative CPU budget cannot cover prior preparation and the first native plan")
      const id = store.db.transaction(() => {
        const prior = store.db.query("SELECT loop,spec_id FROM research_host_bindings WHERE session=?").get(context.sessionID) as Pick<Binding, "loop" | "spec_id"> | null
        if (prior) { if (prior.spec_id !== spec.id) throw Error("Concurrent research scope conflicts"); return prior.loop }
        const loop = store.create(spec.contract)
        // Same transaction as loop creation: insufficient cumulative budget
        // cannot leave a zero-cost loop which a subsequent open could resume.
        priorUsage?.record(store, loop.id)
        store.db.query("INSERT INTO research_host_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)").run(loop.id, context.sessionID, spec.id,
          JSON.stringify(spec), researchDigest(spec), input.catalog_digest, JSON.stringify(call.source), call.user_digest, call.part_digest,
          JSON.stringify(prepared), researchDigest(prepared))
        store.record(loop.id, "host-registration", { schema: "drug-design-research-host-registration-v1", spec_id: spec.id,
          spec_digest: researchDigest(spec), catalog_digest: input.catalog_digest, request: call.source.origin,
          tool_call_id: call.call_id, policy_digest: researchDigest(spec.policy), verification: proof.review })
        spec.policy_evidence.forEach((item, index) => store.record(loop.id, item.ref, { schema: "research-host-policy-evidence-v1",
          path: item.path, sha256: item.sha256, content: proof.sources[index] }))
        return loop.id
      }).immediate()
      const saved = bound(id, context)
      await registerResearchNativePlan({ research: store, id, key: "round-1", round: 1, prepared: saved.prepared })
      return { step: "opened", resumed: false, ...view(id, context) }
    }
    return step(args.loop_id!, args.action, { sessionID: context.sessionID, messageID: context.messageID,
      callID: call.call_id, abort: context.abort, tool: context })
  },
  /** Runs from the existing project observer, under Instance.provide. It reuses
   * original persisted platform identity and has no approval/start capability. */
  async resume(abort: AbortSignal) {
    if (!catalog) return []
    const rows = store.db.query("SELECT loop,session FROM research_host_bindings WHERE json_extract(spec,'$.contract.directory')=? AND json_extract(spec,'$.workspace_project')=? ORDER BY loop").all(input.directory, input.project) as Pick<Binding, "loop" | "session">[]
    const result = []
    for (const row of rows) {
      abort.throwIfAborted()
      try {
        const value = bound(row.loop, { sessionID: row.session })
        const current = await step(row.loop, "tick", { sessionID: row.session,
          messageID: value.source.origin.assistant_message_id, callID: value.call_id, abort })
        result.push(current)
      } catch (error) {
        result.push({ loop_id: row.loop, step: "needs_attention", reason: error instanceof Error ? error.message : "Research host recovery failed" })
      }
    }
    return result
  } }

  async function step(id: string, action: "read" | "tick" | "recover", context: StepContext) {
    const value = bound(id, context)
    await original(value)
    const priorUsage = await prepareResearchPriorUsage(value.spec.contract)
    priorUsage?.verify(store, id)
    if (action === "read") return { step: "read", ...view(id, context) }
    const identity = { research: store, native: input.native, id }
    if (["FINISHED", "CANCELLED", "BLOCKED"].includes(store.get(id).state)) {
      if (store.evidence(id, "host").some(item => item.key.startsWith("native-selection:"))
        && readResearchNativeAction(identity).observation_required) await observeResearchNativeAction(identity)
      return { step: "stopped", ...view(id, context) }
    }
    if (!catalog) return { step: "needs_attention", reason: "The installation-verified catalog must be restored before resuming this loop.", ...view(id, context) }
    if (!offered(id)) {
      await registerResearchNativePlan({ research: store, id, key: "round-1", round: 1, prepared: value.prepared })
      return { step: "registration_recovered", ...view(id, context) }
    }
    const outstanding = slots(id).find(slot => !consumed(id, slot.key))
    const pendingEvaluation = store.reservations(id).some(item => item.payload.kind === "evaluation" && item.state === "RESERVED")
    if (pendingEvaluation) {
      const result = input.evaluation ? await input.evaluation.recover({ ...identity, abort: context.abort }) : null
      return { step: "scientific_evaluation_recovery", result, ...view(id, context) }
    }
    if (outstanding || store.get(id).state === "EVALUATING") {
      const result = await recoverResearchLiveDecision({ store, id, key: outstanding?.key ?? "host-recover-local" })
      return { step: result.status === "no_attempt" ? "needs_reconciliation" : "model_or_local_action_recovered",
        reason: result.status === "no_attempt" ? "A durable model slot has no provider attempt; do not automatically reissue it." : undefined,
        result, ...view(id, context) }
    }
    const selected = store.evidence(id, "host").some(item => item.key.startsWith("native-selection:"))
    const native = selected ? readResearchNativeAction(identity) : null
    if (native?.observation_required) {
      const before = new Set(store.evidence(id, "observation").map(item => item.key))
      const observed = await observeResearchNativeAction(identity)
      if (observed.evidence_ref && !before.has(observed.evidence_ref)) return { step: "observed", ...view(id, context) }
      if (native.status === "SUCCEEDED" && value.spec.contract.allowed_adaptations.includes("evaluate")
        && !["FINISHED", "CANCELLED", "BLOCKED"].includes(store.get(id).state)) {
        const key = `host-scientific-evaluation:${observed.observation!.id}`
        if (!store.evidence(id, "host").some(item => item.key === key)) {
          if (!input.evaluation) {
            store.record(id, key, { schema: "research-host-evaluation-disposition-v1", status: "unavailable", reason: "No fixed scientific evaluator is installed" })
            return { step: "scientific_evaluator_unavailable", ...view(id, context) }
          }
          if (action !== "recover" && Date.parse(value.spec.valid_until) <= Date.now())
            return { step: "needs_reconciliation", reason: "Frozen host specification has expired before scientific evaluation.", ...view(id, context) }
          await input.authorizeEvaluation()
          const result = await (action === "recover" ? input.evaluation.recover : input.evaluation.evaluate)({ ...identity, decision_id: native.decision_id, abort: context.abort }).catch(error => {
            if (context.abort.aborted) throw error
            return { status: "rejected", evaluation_ref: null, reason: error instanceof Error ? error.message : "Scientific evaluation rejected" }
          })
          if (["completed", "rejected"].includes(result.status)) store.record(id, key, { schema: "research-host-evaluation-disposition-v1", status: result.status, evaluation_ref: result.evaluation_ref ?? null, reason: result.reason ?? null })
          return { step: "scientific_evaluation", result, ...view(id, context) }
        }
      }
    }
    const loop = store.get(id)
    if (["FINISHED", "CANCELLED", "BLOCKED"].includes(loop.state)) return { step: "stopped", ...view(id, context) }
    if (action === "recover") {
      const result = input.evaluation && native ? await input.evaluation.recover({ ...identity, decision_id: native.decision_id, abort: context.abort }) : null
      return { step: "recovered", evaluation: result, ...view(id, context) }
    }
    context.abort.throwIfAborted()
    if (Date.parse(value.spec.valid_until) <= Date.now()) return { step: "needs_reconciliation", reason: "Frozen host specification has expired; existing evidence is retained.", ...view(id, context) }
    if (native && ["PREPARING", "AWAITING_APPROVAL", "RUNNING"].includes(loop.state)) {
      if (native.status === "not_prepared" || native.status === "DRAFT") {
        if (!context.tool) return { step: "needs_attention", reason: "This exact plan requires the platform's native permission flow.", ...view(id, context) }
        await input.authorizeRemote()
        const result = await advanceResearchNativeAction({ ...identity, context: context.tool, phase: "approve", decision_id: native.decision_id })
        return { step: "native_permission", result, ...view(id, context) }
      }
      if (["APPROVED", "ACTIVE", "NEEDS_REVIEW"].includes(native.status) && !attached.has(native.task_id)) {
        if (!context.tool) return { step: native.status === "APPROVED" ? "needs_attention" : "waiting_for_native_result", reason: "Background observation never starts or reattaches a native supervisor.", ...view(id, context) }
        await input.authorizeRemote()
        const result = await advanceResearchNativeAction({ ...identity, context: context.tool, phase: "start", decision_id: native.decision_id })
        if (result.result.state === "observer_ready") attached.add(native.task_id)
        return { step: "native_supervisor", result, ...view(id, context) }
      }
      return { step: "waiting_for_native_result", ...view(id, context) }
    }
    if (!["READY", "DECIDING"].includes(loop.state)) return { step: "needs_reconciliation", ...view(id, context) }
    await policy(value.spec)
    const user = await original(value)
    const claim = store.db.transaction(() => {
      const prior = slots(id).find(slot => !consumed(id, slot.key))
      if (prior) return { created: false, slot: prior }
      const ordinal = slots(id).length + 1, key = `host-model-${ordinal}`
      store.db.query("INSERT INTO research_host_model_slots VALUES(?,?,?,?,?)").run(id, ordinal, key, context.messageID, context.callID)
      return { created: true, slot: { loop: id, ordinal, key, message: context.messageID, call: context.callID! } }
    }).immediate()
    if (!claim.created) return { step: "model_claim_already_exists", ...view(id, context) }
    const { Provider } = await import("../../src/provider/provider")
    const { Agent } = await import("../../src/agent/agent")
    const { SystemPrompt } = await import("../../src/session/system")
    const model = await Provider.getModel(value.spec.policy.provider_id, value.spec.policy.model_id)
    const agent = await Agent.get("research")
    if (!agent) throw Error("Installed research agent is unavailable; model slot retained for reconciliation")
    const result = await runResearchLiveDecision({ store, id, key: claim.slot.key, policy: value.spec.policy,
      request: { user, sessionID: context.sessionID, model, agent, abort: context.abort,
        system: await SystemPrompt.environment(model, context.sessionID), limits: value.spec.limits } })
    return { step: "model_decision", result, ...view(id, context) }
  }
}
