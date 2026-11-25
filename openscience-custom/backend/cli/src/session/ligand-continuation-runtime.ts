import z from "zod"
import { Instance } from "../project/instance"
import { Scheduler } from "../scheduler"
import { Storage } from "../storage/storage"
import { Session } from "."
import { SessionStatus } from "./status"
import { Log } from "../util/log"
import { acknowledgeLigandContinuation, armLigandContinuation, readLigandContinuation, tickLigandContinuation, type LigandContinuation } from "./ligand-continuation"

const log = Log.create({ service: "ligand-continuation" })
const starts = new Set(["start_receptor_preparation", "start_vs", "start_covdock"])

function payload(output: unknown) {
  const envelope = z.object({ isError: z.boolean().optional(), structuredContent: z.unknown().optional(),
    content: z.array(z.object({ type: z.string(), text: z.string().optional() })).optional() }).parse(output)
  if (envelope.isError) throw Error("Scientific connector returned an error")
  return envelope.structuredContent ?? JSON.parse(envelope.content?.find(item => item.type === "text")?.text ?? "null")
}

export async function registerLigandContinuation(scope: { project: string; session: string },
  entry: string, action: string, output: unknown) {
  if (action === "collect") {
    const value = z.object({ route: z.literal("ligand-first"),
      workflow: z.object({ workflow_id: z.string() }),
      evidence_id: z.string().regex(/^hyp-[a-f0-9]{24}$/),
      result: z.object({ evidence_id: z.string(), state: z.literal("RUNNING") }),
    }).parse(payload(output))
    if (value.evidence_id !== value.result.evidence_id) throw Error("Evidence collection identity changed")
    // For evidence, both durable identity slots refer to the collection, never a compute request.
    return armLigandContinuation(scope, { kind: "evidence", entry, workflow_id: value.workflow.workflow_id,
      run_id: value.evidence_id, request_id: value.evidence_id })
  }
  if (!starts.has(action)) {
    const saved = await readLigandContinuation(scope)
    if (!saved) return
    if (saved.kind === "evidence") {
      const value = z.object({ route: z.literal("ligand-first"),
        workflow: z.object({ workflow_id: z.string() }),
        result: z.object({ evidence_id: z.string(), state: z.string() }),
      }).safeParse(payload(output))
      if (value.success)
        await acknowledgeLigandContinuation(scope, { kind: "evidence", entry,
          workflow_id: value.data.workflow.workflow_id, run_id: value.data.result.evidence_id,
          request_id: value.data.result.evidence_id }, value.data.result.state)
      return
    }
    const value = z.object({ route: z.literal("ligand-first"),
      workflow: z.object({ workflow_id: z.string() }),
      result: z.object({ run_id: z.string(), job_state: z.string() }),
    }).safeParse(payload(output))
    if (value.success)
      await acknowledgeLigandContinuation(scope, { entry, request_id: saved.request_id,
        workflow_id: value.data.workflow.workflow_id, run_id: value.data.result.run_id }, value.data.result.job_state)
    return
  }
  const value = z.object({ route: z.literal("ligand-first"), pending: z.unknown().nullish(),
    workflow: z.object({ workflow_id: z.string() }), covdock_execution: z.unknown().optional(),
    vs_execution: z.unknown().optional(), refinement_input: z.record(z.string(), z.unknown()).nullish(),
    vs_receptor: z.record(z.string(), z.unknown()).nullish(),
  }).parse(payload(output))
  if (value.pending) throw Error("Submission outcome unknown; cannot register a different task")
  const raw = action === "start_covdock" ? value.covdock_execution : action === "start_vs" ? value.vs_execution
    : value.refinement_input?.receptor_preparation ?? value.vs_receptor?.receptor_preparation
  const task = z.object({ run_id: z.string(), request_id: z.string(),
    job_state: z.enum(["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"]) }).parse(raw)
  return armLigandContinuation(scope, { entry, workflow_id: value.workflow.workflow_id,
    run_id: task.run_id, request_id: task.request_id })
}

async function active(session: string, saved: LigandContinuation) {
  const binding = await Storage.read<{ active?: boolean; entry: string; workflow_id: string }>(
    ["ligand-workflow-bindings", Instance.project.id, session])
  const info = await Session.get(session)
  return info.projectID === Instance.project.id && binding.active !== false
    && binding.entry === saved.entry && binding.workflow_id === saved.workflow_id
}

/** The queued system notification is durable before inference starts. */
export async function queueLigandNotification(session: string, saved: LigandContinuation) {
  const messages = await Session.messages({ sessionID: session })
  const answered = messages.some(item => item.info.role === "assistant"
    && item.info.parentID === saved.messageID && (!!item.info.finish || !!item.info.error))
  if (answered) return false
  const existing = messages.find(item => item.info.id === saved.messageID)
  if (!existing?.parts.some(part => part.type === "text" && part.synthetic)) {
    const parent = messages.findLast(item => item.info.role === "user" && item.info.id !== saved.messageID)
    if (!parent || parent.info.role !== "user") throw Error("Original session request is unavailable")
    await Session.updateMessage({ role: "user", id: saved.messageID, sessionID: session,
      time: { created: Date.now() }, model: parent.info.model, agent: parent.info.agent,
      effort: parent.info.effort, tools: parent.info.tools, system: parent.info.system,
      variant: parent.info.variant, tier: parent.info.tier })
    await Session.updatePart({ id: "prt_" + saved.messageID.slice(4), sessionID: session, messageID: saved.messageID,
      type: "text", synthetic: true,
      text: `System task notification: existing Ligand-first task ${saved.run_id} is ${saved.observed}. Continue the original request using the bound workflow. Refresh this exact task, evaluate its evidence and deliver its report when permitted. A failed task is not a molecular failure. Do not resubmit it or infer new compute approval from this notification. Respect the user's stopping stage and all current permissions.` })
  }
  return true
}

export async function wakeLigandSession(session: string, saved: LigandContinuation) {
  if (!await queueLigandNotification(session, saved)) return
  const { SessionPrompt } = await import("./prompt")
  void SessionPrompt.loop(session).catch(error => log.error("continuation inference failed", { session, error }))
}

export function initLigandContinuations() {
  const directory = Instance.directory
  Scheduler.register({ id: "ligand-continuations", interval: 60_000,
    run: async () => { await Instance.provide({ directory, fn: async () => {
      const keys = await Storage.list(["ligand-continuations", Instance.project.id])
      for (const key of keys) {
        if (key.length !== 3) continue
        const session = key[2]
        await tickLigandContinuation({ project: Instance.project.id, session }, {
          active: saved => active(session, saved),
          idle: () => SessionStatus.get(session).type === "idle",
          observe: async saved => {
            const { MCP } = await import("../mcp")
            const tools = await MCP.tools()
            const prefix = saved.entry.slice(0, -"covalent_ligand_workflow".length)
            if (saved.kind === "evidence") {
              const read = tools[prefix + "covalent_target_hypothesis"]?.execute
              if (!read) throw Error("Bound evidence observer unavailable")
              return payload(await read({ action: "read", evidence_id: saved.run_id },
                { toolCallId: "ligand-continuation-evidence-observe", messages: [] }))
            }
            const observe = tools[prefix + "covalent_get_job"]?.execute
            if (!observe) throw Error("Bound research connector unavailable")
            return payload(await observe({ run_id: saved.run_id },
              { toolCallId: "ligand-continuation-observe", messages: [] }))
          },
          wake: saved => wakeLigandSession(session, saved),
        }).catch(error => log.error("continuation check failed", { session, error }))
        // Recover a crash between durable notification delivery and model startup.
        const saved = await readLigandContinuation({ project: Instance.project.id, session })
        if (saved?.state === "delivered" && saved.expires > Date.now()
            && SessionStatus.get(session).type === "idle" && await active(session, saved))
          await wakeLigandSession(session, saved)
      }
    } }) },
  })
}
