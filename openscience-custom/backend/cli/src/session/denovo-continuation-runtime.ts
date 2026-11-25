import z from "zod"
import { Instance } from "../project/instance"
import { Scheduler } from "../scheduler"
import { Storage } from "../storage/storage"
import { Session } from "."
import { SessionStatus } from "./status"
import { Log } from "../util/log"
import { boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { readReasoning, reasoningStatus } from "../../integrations/discovery-platform/study-reasoning"
import { acknowledgeDeNovoContinuation, armDeNovoContinuation, readDeNovoContinuation, resumeDeNovoContinuation, tickDeNovoContinuation, type DeNovoContinuation } from "./denovo-continuation"

const log = Log.create({ service: "denovo-continuation" })
type Scope = { directory: string; project: string; session: string }

export async function resumeDeNovoFromUser(session: string, parts: { type: string; text?: string; synthetic?: boolean; ignored?: boolean }[]) {
  if (!parts.some(part => part.type === "text" && !part.synthetic && !part.ignored && part.text?.trim())) return
  await resumeDeNovoContinuation({ project: Instance.project.id, session })
}

async function bound(scope: Scope, saved: Pick<DeNovoContinuation, "study">) {
  const session = await Session.get(scope.session)
  const study = await boundSessionStudy(scope)
  if (session.projectID !== scope.project || session.directory !== scope.directory
    || !study || study.id !== saved.study || !study.routes["de-novo"].selected
    || study.routes["de-novo"].mode !== "new") return undefined
  return study
}

export async function registerDeNovoReasoning(scope: Scope, study: string, child: string, resume = false) {
  const input = { study, stage: "reasoning" as const, task: child, request: study }
  const plan = await bound(scope, input)
  if (!plan || (await readReasoning(scope.directory, plan))?.session !== child)
    throw Error("Reasoning continuation differs from the bound study")
  return armDeNovoContinuation(scope, input, Date.now(), resume)
}

export async function observeDeNovoContinuation(scope: Scope, saved: DeNovoContinuation) {
  const study = await bound(scope, saved)
  if (!study) throw Error("De novo continuation study is no longer bound")
  if (saved.stage !== "reasoning") {
    const { observeDeNovoCompute } = await import("./denovo-continuation-compute")
    return observeDeNovoCompute(scope, saved)
  }
  const result = await reasoningStatus(scope.directory, study)
  if (result.session !== saved.task || saved.request !== study.id)
    throw Error("Reasoning continuation task changed")
  const state = result.live ? "RUNNING" : result.state === "finished" ? "COMPLETED"
    : result.state === "failed" ? "FAILED" : "INTERRUPTED"
  return { study: saved.study, stage: saved.stage, task: saved.task, request: saved.request, state }
}

export async function acknowledgeDeNovoReasoning(scope: Scope, output: unknown) {
  const result = z.object({ state: z.string(), session: z.string().nullable(), live: z.boolean().optional() }).parse(output)
  if (result.live || !["finished", "failed", "interrupted_or_not_dispatched"].includes(result.state)) return
  const saved = await readDeNovoContinuation(scope)
  if (!saved || saved.stage !== "reasoning" || result.session !== saved.task || !await bound(scope, saved)) return
  await acknowledgeDeNovoContinuation(scope, { study: saved.study, stage: saved.stage, task: saved.task, request: saved.request },
    result.state === "finished" ? "COMPLETED" : result.state === "failed" ? "FAILED" : "INTERRUPTED")
}

/** A frozen plan has consumed the finalized proposal even if the child is still writing its summary. */
export async function consumeDeNovoReasoning(scope: Scope, studyID: string, task: string) {
  const saved = await readDeNovoContinuation(scope)
  if (!saved || saved.stage !== "reasoning") return
  const study = await bound(scope, saved)
  if (!study || study.id !== studyID) throw Error("Consumed reasoning study changed")
  const { readDeNovoPlan } = await import("../../integrations/discovery-platform/study-denovo-plan")
  const plan = await readDeNovoPlan(scope.directory, study)
  const reasoning = await readReasoning(scope.directory, study)
  if (!plan || plan.plan.request.id !== task || plan.plan.request.session_id !== saved.task
    || reasoning?.session !== saved.task || saved.request !== studyID)
    throw Error("Consumed reasoning differs from the frozen generation plan")
  await acknowledgeDeNovoContinuation(scope, { study: studyID, stage: "reasoning", task: saved.task, request: studyID }, "COMPLETED")
}

export async function queueDeNovoNotification(session: string, saved: DeNovoContinuation) {
  const messages = await Session.messages({ sessionID: session })
  if (messages.some(item => item.info.role === "assistant" && item.info.parentID === saved.messageID
    && (!!item.info.finish || !!item.info.error))) return false
  const existing = messages.find(item => item.info.id === saved.messageID)
  if (!existing?.parts.some(part => part.type === "text" && part.synthetic)) {
    const parent = messages.findLast(item => item.info.role === "user" && item.info.id !== saved.messageID)
    if (!parent || parent.info.role !== "user") throw Error("Original De novo request is unavailable")
    await Session.updateMessage({ role: "user", id: saved.messageID, sessionID: session,
      time: { created: Date.now() }, model: parent.info.model, agent: parent.info.agent,
      effort: parent.info.effort, tools: parent.info.tools, system: parent.info.system,
      variant: parent.info.variant, tier: parent.info.tier })
    await Session.updatePart({ id: "prt_" + saved.messageID.slice(4), sessionID: session, messageID: saved.messageID,
      type: "text", synthetic: true,
      text: `System task notification: the original De novo study ${saved.study}, stage ${saved.stage}, task ${saved.task} is ${saved.observed}. Continue the bound original request. For completed reasoning, inspect reasoning-status, sync-hypotheses and compile the frozen generation plan. For completed compute, inspect that exact task and continue evidence evaluation and reporting. RECONCILE means execution liveness needs verification, not a failed calculation: call the existing batch status action for this exact task to reconcile its already signed run, then wait on that same run. Never call a submit tool to recover it. Report failures or interruptions accurately; do not recreate the study, resubmit compute, or infer execution authorization from this notification. All native approvals and user stopping instructions remain required.` })
  }
  return true
}

async function wake(session: string, saved: DeNovoContinuation) {
  if (!await queueDeNovoNotification(session, saved)) return
  const { SessionPrompt } = await import("./prompt")
  void SessionPrompt.loop(session).catch(error => log.error("continuation inference failed", { session, error }))
}

export function initDeNovoContinuations() {
  const directory = Instance.directory
  Scheduler.register({ id: "denovo-continuations", interval: 60_000,
    run: async () => Instance.provide({ directory, fn: async () => {
      for (const key of await Storage.list(["denovo-continuations", Instance.project.id])) {
        if (key.length !== 3) continue
        const scope = { directory, project: Instance.project.id, session: key[2] }
        try {
          await tickDeNovoContinuation(scope, {
            active: async saved => !!await bound(scope, saved),
            idle: () => SessionStatus.get(scope.session).type === "idle",
            observe: saved => observeDeNovoContinuation(scope, saved),
            wake: saved => wake(scope.session, saved),
          })
        } catch (error) { log.error("continuation check failed", { session: scope.session, error }) }
      }
    } }),
  })
}
