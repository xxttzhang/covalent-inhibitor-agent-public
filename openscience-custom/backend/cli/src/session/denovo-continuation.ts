import z from "zod"
import { isDeepStrictEqual } from "node:util"
import { Storage } from "../storage/storage"
import { Lock } from "../util/lock"
import { Identifier } from "../id/id"

const identity = z.object({
  study: z.string().startsWith("study-"),
  stage: z.enum(["reasoning", "generation", "vs", "lo", "glide"]),
  task: z.string().min(1),
  request: z.string().min(1),
}).strict()
const record = identity.extend({
  state: z.enum(["waiting", "ready", "delivered", "observed", "paused"]),
  messageID: z.string().startsWith("msg_"),
  next: z.number(), expires: z.number(), observed: z.string().optional(), error: z.string().optional(),
})
export type DeNovoContinuation = z.infer<typeof record>
type Scope = { project: string; session: string }
const key = (scope: Scope) => ["denovo-continuations", scope.project, scope.session]
const stopKey = (scope: Scope) => ["denovo-continuation-stops", scope.project, scope.session]
const stopped = (scope: Scope) => Storage.read(stopKey(scope)).catch(error => {
  if (error instanceof Storage.NotFoundError) return undefined
  throw error
})

export async function readDeNovoContinuation(scope: Scope) {
  const value = await Storage.read(key(scope)).catch(error => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  })
  return value === undefined ? undefined : record.parse(value)
}

/** The caller verifies the native study and original task before registration. */
export async function armDeNovoContinuation(scope: Scope, input: z.infer<typeof identity>, now = Date.now(), resume = false) {
  const bound = identity.parse(input)
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readDeNovoContinuation(scope)
  if (saved && isDeepStrictEqual(identity.parse({ study: saved.study, stage: saved.stage,
    task: saved.task, request: saved.request }), bound)) {
    if (!resume || !["observed", "delivered"].includes(saved.state)) return saved
    const value: DeNovoContinuation = { ...saved, state: "waiting", next: now, observed: undefined,
      expires: now + 24 * 60 * 60_000, error: undefined }
    await Storage.write(key(scope), value)
    return value
  }
  if (saved && ["waiting", "ready", "paused"].includes(saved.state))
    throw Error("Resolve the original De novo continuation before replacing it")
  const value: DeNovoContinuation = { ...bound, state: await stopped(scope) ? "paused" : "waiting", next: now,
    expires: now + 24 * 60 * 60_000, messageID: Identifier.ascending("message") }
  await Storage.write(key(scope), value)
  return value
}

export async function pauseDeNovoContinuation(scope: Scope) {
  using _ = await Lock.write(key(scope).join("/"))
  await Storage.write(stopKey(scope), { stoppedAt: Date.now() })
  const saved = await readDeNovoContinuation(scope)
  if (saved) await Storage.write(key(scope), { ...saved, state: "paused" })
}

/** Only a new real user turn clears stop; synthetic task notifications do not. */
export async function resumeDeNovoContinuation(scope: Scope) {
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readDeNovoContinuation(scope)
  if (saved?.state === "paused") {
    const now = Date.now()
    const terminal = ["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"].includes(saved.observed ?? "")
    await Storage.write(key(scope), terminal
      ? { ...saved, state: "observed", error: undefined }
      : { ...saved, state: "waiting", next: now, expires: now + 24 * 60 * 60_000, error: undefined })
  }
  if (await stopped(scope)) await Storage.remove(stopKey(scope))
}

export async function acknowledgeDeNovoContinuation(scope: Scope, input: z.infer<typeof identity>, observed: string) {
  if (!["RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"].includes(observed)) return
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readDeNovoContinuation(scope)
  if (!saved) return
  if (!isDeepStrictEqual({ study: saved.study, stage: saved.stage, task: saved.task, request: saved.request }, identity.parse(input))) return
  if (observed === "RUNNING") {
    if (["observed", "delivered"].includes(saved.state) && ["RECONCILE", "INTERRUPTED"].includes(saved.observed ?? ""))
      await Storage.write(key(scope), { ...saved, state: "waiting", next: Date.now() + 60_000, observed, error: undefined })
    return
  }
  if (!["waiting", "ready"].includes(saved.state)) return
  await Storage.write(key(scope), { ...saved, state: "observed", observed, error: undefined })
}

/** Observer ports cannot submit or approve computation. Wake deduplicates by messageID. */
export async function tickDeNovoContinuation(scope: Scope, ports: {
  active: (saved: DeNovoContinuation) => Promise<boolean>
  idle: () => boolean
  observe: (saved: DeNovoContinuation) => Promise<unknown>
  wake: (saved: DeNovoContinuation) => Promise<void>
}, now = Date.now()) {
  using _ = await Lock.write(key(scope).join("/"))
  const saved = await readDeNovoContinuation(scope)
  if (!saved || !["waiting", "ready", "delivered"].includes(saved.state) || saved.next > now) return
  if (saved.expires <= now || !await ports.active(saved)) {
    await Storage.write(key(scope), { ...saved, state: "paused", error: "Expired or study no longer active" })
    return
  }
  if (!ports.idle()) return
  try {
    if (saved.state === "delivered") {
      await ports.wake(saved)
      await Storage.write(key(scope), { ...saved, next: now + 60_000, error: undefined })
      return
    }
    if (saved.state === "waiting") {
      const result = z.object({ study: z.literal(saved.study), stage: z.literal(saved.stage),
        task: z.literal(saved.task), request: z.literal(saved.request),
        state: z.enum(["RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "RECONCILE"]),
      }).strict().parse(await ports.observe(saved))
      saved.observed = result.state
      saved.state = result.state === "RUNNING" ? "waiting" : "ready"
      if (saved.state === "ready") saved.messageID = Identifier.ascending("message")
      saved.next = now + 60_000
      saved.error = undefined
      await Storage.write(key(scope), saved)
    }
    if (saved.state !== "ready" || !ports.idle() || !await ports.active(saved)) return
    await ports.wake(saved)
    await Storage.write(key(scope), { ...saved, state: "delivered", next: now + 60_000, error: undefined })
  } catch (error) {
    await Storage.write(key(scope), { ...saved, next: now + 60_000,
      error: error instanceof Error ? error.message.slice(0, 500) : "Observation unavailable" })
  }
}
