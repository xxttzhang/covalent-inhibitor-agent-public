import z from "zod"
import { Storage } from "../storage/storage"
import { Lock } from "../util/lock"
import { Identifier } from "../id/id"

const identity = z.object({
  kind: z.enum(["compute", "evidence"]).optional(),
  entry: z.string().endsWith("covalent_ligand_workflow"),
  workflow_id: z.string().regex(/^ligand-flow-[a-f0-9]{24}$/),
  run_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$/),
  request_id: z.string().min(1),
}).strict()
const record = identity.extend({
  state: z.enum(["waiting", "ready", "delivered", "observed", "paused"]),
  messageID: z.string().startsWith("msg_"),
  next: z.number(),
  expires: z.number(),
  observed: z.string().optional(),
  error: z.string().optional(),
})
export type LigandContinuation = z.infer<typeof record>
type Scope = { project: string; session: string }
const key = (scope: Scope) => ["ligand-continuations", scope.project, scope.session]
const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "EVIDENCE_READY", "INTERRUPTED"])

export async function readLigandContinuation(scope: Scope) {
  const saved = await Storage.read(key(scope)).catch(error => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  })
  return saved === undefined ? undefined : record.parse(saved)
}

/** Call only after the host has verified the bound, already submitted task. */
export async function armLigandContinuation(scope: Scope, input: z.infer<typeof identity>, now = Date.now()) {
  const bound = identity.parse(input)
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readLigandContinuation(scope)
  if (saved && saved.run_id === bound.run_id) {
    if (saved.entry !== bound.entry || saved.workflow_id !== bound.workflow_id || saved.request_id !== bound.request_id
        || (saved.kind ?? "compute") !== (bound.kind ?? "compute"))
      throw Error("Continuation task identity changed")
    return saved
  }
  if (saved && ["waiting", "ready"].includes(saved.state))
    throw Error("Observe or pause the original task before replacing its continuation")
  const value: LigandContinuation = { ...bound, state: "waiting", next: now, expires: now + 24 * 60 * 60_000,
    messageID: Identifier.ascending("message") }
  await Storage.write(key(scope), value)
  return value
}

export async function pauseLigandContinuation(scope: Scope) {
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readLigandContinuation(scope)
  if (!saved) return
  await Storage.write(key(scope), { ...saved, state: "paused" })
}

/** A bound tool response has already delivered this task's terminal state. */
export async function acknowledgeLigandContinuation(scope: Scope, input: z.infer<typeof identity>, state: string) {
  if (!terminal.has(state)) return
  const bound = identity.parse(input)
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readLigandContinuation(scope)
  if (!saved || !["waiting", "ready"].includes(saved.state)) return
  if (saved.entry !== bound.entry || saved.workflow_id !== bound.workflow_id
      || saved.run_id !== bound.run_id || saved.request_id !== bound.request_id
      || (saved.kind ?? "compute") !== (bound.kind ?? "compute")) return
  await Storage.write(key(scope), { ...saved, state: "observed", observed: state, error: undefined })
}

export async function resumeLigandContinuation(scope: Scope) {
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readLigandContinuation(scope)
  if (!saved || saved.state !== "paused") throw Error("No paused task continuation")
  await Storage.write(key(scope), { ...saved, state: "waiting", next: Date.now(),
    expires: Date.now() + 24 * 60 * 60_000, error: undefined })
}

/** Read-only polling. Wake must durably deduplicate by messageID across crashes.
 * No submit, cancel, approval or scientific-evaluation operation is accepted here. */
export async function tickLigandContinuation(scope: Scope, ports: {
  active: (saved: LigandContinuation) => Promise<boolean>
  idle: () => boolean
  observe: (saved: LigandContinuation) => Promise<unknown>
  wake: (saved: LigandContinuation) => Promise<void>
}, now = Date.now()) {
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readLigandContinuation(scope)
  if (!saved || !["waiting", "ready"].includes(saved.state) || saved.next > now) return
  if (saved.expires <= now || !await ports.active(saved)) {
    await Storage.write(key(scope), { ...saved, state: "paused", error: "Expired or workflow no longer active" })
    return
  }
  if (!ports.idle()) return
  try {
    if (saved.state === "waiting") {
      const raw = await ports.observe(saved)
      const observed = saved.kind === "evidence" ? z.object({
        evidence_id: z.literal(saved.run_id),
        state: z.enum(["RUNNING", "EVIDENCE_READY", "FAILED", "INTERRUPTED"]),
      }).parse(raw).state : z.object({
        status: z.literal("PASS"),
        spec: z.object({ run_id: z.literal(saved.run_id), request_id: z.literal(saved.request_id),
          route: z.literal("ligand-first") }),
        job: z.object({ run_id: z.literal(saved.run_id), route: z.literal("ligand-first"),
          state: z.enum(["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"]) }),
      }).parse(raw).job.state
      saved.observed = observed
      saved.error = undefined
      saved.state = terminal.has(observed) ? "ready" : "waiting"
      if (saved.state === "ready") saved.messageID = Identifier.ascending("message")
      saved.next = now + 60_000
      await Storage.write(key(scope), saved)
    }
    if (saved.state !== "ready" || !ports.idle() || !await ports.active(saved)) return
    await ports.wake(saved)
    await Storage.write(key(scope), { ...saved, state: "delivered", error: undefined })
  } catch (error) {
    // A failed observation is unknown state, never a reason to resubmit compute.
    await Storage.write(key(scope), { ...saved, next: now + 60_000,
      error: error instanceof Error ? error.message.slice(0, 500) : "Continuation unavailable" })
  }
}
