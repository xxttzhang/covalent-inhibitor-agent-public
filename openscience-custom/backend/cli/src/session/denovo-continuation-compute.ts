import path from "node:path"
import { lstat, realpath } from "node:fs/promises"
import { createHash } from "node:crypto"
import z from "zod"
import { boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { readDeNovoPlan } from "../../integrations/discovery-platform/study-denovo-plan"
import { deNovoTasks, readDeNovoTask } from "../tool/denovo-task"
import { batchLOFile } from "../tool/denovo-selection"
import { boundBatchSubmission } from "./denovo-batch-execution"
import { acknowledgeDeNovoContinuation, armDeNovoContinuation, type DeNovoContinuation } from "./denovo-continuation"

type Scope = { directory: string; project: string; session: string }
type Stage = "generation" | "vs" | "lo" | "glide"

export function generationState(state: string) {
  if (["COMPLETED", "FAILED", "CANCELLED"].includes(state)) return state
  if (["NEEDS_REVIEW", "OUTCOME_UNKNOWN"].includes(state)) return "INTERRUPTED"
  if (["AWAITING_APPROVAL", "PREPARING", "SUBMITTING", "QUEUED", "GENERATING", "DOWNLOADING", "VALIDATING", "PUBLISHING"].includes(state)) return "RUNNING"
  throw Error("Unknown generation state; retain the original task")
}

async function original(scope: Scope, studyID: string, task: string) {
  const study = await boundSessionStudy(scope)
  if (!study || study.id !== studyID || !study.routes["de-novo"].selected || study.routes["de-novo"].mode !== "new")
    throw Error("De novo compute continuation study changed")
  const plan = await readDeNovoPlan(scope.directory, study)
  if (!plan || plan.plan.request.id !== task) throw Error("Continuation task differs from the frozen generation plan")
  const status = await readDeNovoTask(deNovoTasks, task, { directory: scope.directory,
    session: scope.session, study: study.id, request: plan.plan.request })
  if (!status || !status.approval_verified) throw Error("Original generation approval is unavailable")
  return status
}

async function batchRequest(task: string, stage: Exclude<Stage, "generation">) {
  const filename = stage === "lo" ? await batchLOFile(task, "plan")
    : path.join(deNovoTasks, task, stage === "vs" ? "batch-vs-plan.json" : "batch-glide-plan.json")
  if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
    throw Error("Continuation requires the original regular frozen batch plan")
  return boundBatchSubmission(await Bun.file(filename).json(), task, stage)
}

export async function registerDeNovoCompute(scope: Scope, study: string, stage: Stage, task: string, request: string) {
  await original(scope, study, task)
  const expected = stage === "generation" ? task : (await batchRequest(task, stage)).request_id
  if (expected !== request) throw Error("Continuation request differs from the frozen plan")
  if (stage === "generation") {
    const { consumeDeNovoReasoning } = await import("./denovo-continuation-runtime")
    await consumeDeNovoReasoning(scope, study, task)
  }
  return armDeNovoContinuation(scope, { study, stage, task, request })
}

export async function acknowledgeDeNovoCompute(scope: Scope, stage: Stage, task: string, request: string, state: string) {
  const study = await boundSessionStudy(scope)
  if (!study || !study.routes["de-novo"].selected) return
  await acknowledgeDeNovoContinuation(scope, { study: study.id, stage, task, request },
    stage === "generation" ? generationState(state) : state === "QUEUED" ? "RUNNING" : state)
}

function payload(output: unknown) {
  const value = z.object({ isError: z.boolean().optional(), structuredContent: z.unknown().optional(),
    content: z.array(z.object({ type: z.string(), text: z.string().optional() })).optional() }).parse(output)
  if (value.isError) throw Error("De novo observation connector returned an error")
  return value.structuredContent ?? JSON.parse(value.content?.find(item => item.type === "text")?.text ?? "null")
}

export function registeredBatchState(saved: DeNovoContinuation, run: string, output: unknown) {
  const project = "de-flow-" + createHash("sha256").update(saved.task).digest("hex").slice(0, 24)
  const engine = saved.stage === "glide" ? "de-novo-prereaction-glide" : `schrodinger-covdock-${saved.stage}`
  const value = z.object({ status: z.literal("PASS"),
    spec: z.object({ run_id: z.literal(run), request_id: z.literal(saved.request), project_id: z.literal(project),
      route: z.literal("de-novo"), engine: z.literal(engine), execution_mode: z.literal("execute") }),
    job: z.object({ run_id: z.literal(run), project_id: z.literal(project), route: z.literal("de-novo"),
      engine: z.literal(engine), state: z.enum(["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"]) }),
    worker_observation: z.object({ reconciliation_required: z.boolean() }).optional(),
  }).parse(output)
  if (value.worker_observation?.reconciliation_required || value.job.state === "QUEUED") return "RECONCILE"
  return value.job.state
}

export async function observeDeNovoCompute(scope: Scope, saved: DeNovoContinuation) {
  const status = await original(scope, saved.study, saved.task)
  const identity = { study: saved.study, stage: saved.stage, task: saved.task, request: saved.request }
  if (saved.stage === "generation") {
    if (saved.request !== saved.task) throw Error("Generation continuation request changed")
    return { ...identity, state: generationState(status.state) }
  }
  if (saved.stage === "reasoning") throw Error("Not a compute continuation")
  const request = await batchRequest(saved.task, saved.stage)
  if (request.request_id !== saved.request) throw Error("Observed batch request changed")
  const { MCP } = await import("../mcp")
  const tools = await MCP.tools()
  const lookup = tools["covalent-discovery_covalent_lookup_submission"]?.execute
  const get = tools["covalent-discovery_covalent_get_job"]?.execute
  if (!lookup || !get) throw Error("Read-only De novo task observers unavailable")
  const found = z.object({ status: z.literal("PASS"),
    identity: z.object({ project_id: z.literal(request.project_id), request_id: z.literal(saved.request), execution_mode: z.literal("execute") }),
    registered_job_observed: z.boolean().optional(), run_id: z.string().optional(),
  }).parse(payload(await lookup({ project_id: request.project_id, request_id: saved.request, execution_mode: "execute" },
    { toolCallId: "denovo-continuation-lookup", messages: [] })))
  if (!found.registered_job_observed || !found.run_id)
    return { ...identity, state: "INTERRUPTED" }
  const observed = payload(await get({ run_id: found.run_id }, { toolCallId: "denovo-continuation-observe", messages: [] }))
  return { ...identity, state: registeredBatchState(saved, found.run_id, observed) }
}
