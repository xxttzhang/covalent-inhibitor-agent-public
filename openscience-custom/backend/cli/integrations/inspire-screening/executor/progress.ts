import { digest, outcome, type Task } from "./model"
import type { Store } from "./store"
import z from "zod"
import { publications } from "./publication"
import { deliveryStatus } from "./delivery-queue"
import { selectionSnapshot } from "./selection-proposal"
import { preparationSnapshot } from "./preparation-diagnostic"

const part = z.object({ id: z.string(), sessionID: z.string(), messageID: z.string(),
  type: z.literal("tool"), tool: z.literal("inspire_screening_task"),
  state: z.object({ status: z.enum(["completed", "error"]), input: z.record(z.string(), z.unknown()),
    output: z.string().optional(), metadata: z.record(z.string(), z.unknown()).optional() }).passthrough(),
}).passthrough()
type Binding = { message_id: string; part_id: string; fingerprint: string | null; retry_after: number }

export function progress(store: Store, task: Task) {
  const stages = store.stages(task.id)
  const files = publications(store, task)
  return { version: 1, task_id: task.id, plan_digest: task.digest, state: task.state,
    target: `${task.plan.target} ${task.plan.site}`, library: task.plan.library,
    error: task.error ?? stages.find((stage) => stage.error)?.error ?? null,
    results: files.slice(0, 100), results_total: files.length,
    delivery: deliveryStatus(store, task),
    selection: selectionSnapshot(store, task),
    preparation_diagnostic: preparationSnapshot(store, task),
    stages: stages.map((stage) => {
      const result = stage.manifest ? outcome.parse(JSON.parse(stage.manifest)) : null
      return { phase: stage.phase, state: stage.state, job: stage.handle,
        input_count: result?.input_count ?? null, success_count: result?.success_count ?? null,
        failed_count: result?.failed_count ?? null, error: stage.error }
    }),
    note: "后台任务状态，不是新计算或资源批准。核心流程不包含 AF3/CovDock；领取结果前仍需报告文件复核。" }
}

// Persisted metadata only: never changes original tool input/output, approval,
// task state, model text, or job state. No model invocation and no compute API.
export class Progress {
  constructor(readonly store: Store, readonly base = "http://127.0.0.1:4096", readonly fetcher: (request: Request) => Promise<Response> = fetch) {
    const url = new URL(base)
    if (!["127.0.0.1", "localhost", "openscience.internal"].includes(url.hostname)) throw new Error("Progress delivery must stay on the local platform")
    store.db.exec(`CREATE TABLE IF NOT EXISTS presentation(task_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
      part_id TEXT NOT NULL, fingerprint TEXT, retry_after INTEGER NOT NULL DEFAULT 0, error TEXT)`)
  }
  async request(task: Task, suffix: string, init?: RequestInit) {
    const response = await this.fetcher(new Request(`${this.base}/session/${encodeURIComponent(task.plan.session)}${suffix}?directory=${encodeURIComponent(task.plan.directory)}${suffix === "/message" ? "&limit=100" : ""}`,
      { ...init, signal: AbortSignal.timeout(5000), redirect: "error" }))
    if (!response.ok) throw new Error(`Platform progress HTTP ${response.status}`)
    return response.json() as Promise<unknown>
  }
  matches(value: unknown, task: Task) {
    const parsed = part.safeParse(value)
    if (!parsed.success || parsed.data.sessionID !== task.plan.session ||
      !["create", "approve"].includes(String(parsed.data.state.input.action))) return undefined
    const state = parsed.data.state
    const payload = (() => { try { return JSON.parse(state.output ?? "null") } catch { return null } })()
    if (state.status === "completed" && (payload?.task_id !== task.id || payload?.digest !== task.digest)) return undefined
    if (state.status === "error" && (state.metadata?.task_id !== task.id || state.metadata?.digest !== task.digest)) return undefined
    return parsed.data
  }
  async publish(task: Task) {
    if (!task.approval) return { delivered: false, reason: "unapproved" }
    if (JSON.parse(task.approval).digest !== task.digest) throw new Error("Progress approval identity mismatch")
    const binding = this.store.db.query("SELECT * FROM presentation WHERE task_id=?").get(task.id) as Binding | null
    const value = progress(this.store, task)
    const fingerprint = digest(value)
    if (binding?.fingerprint === fingerprint) return { delivered: false, reason: "unchanged" }
    if (binding && binding.retry_after > Date.now()) return { delivered: false, reason: "backoff" }
    try {
      const messageID = binding?.message_id || task.plan.origin?.assistant_message_id
      // Legacy tasks lack origin. Search only the latest 100 messages once;
      // subsequent updates use the persisted exact message/part binding.
      const messages = messageID ? [await this.request(task, `/message/${encodeURIComponent(messageID)}`)]
        : await this.request(task, "/message")
      const parsed = z.array(z.object({ info: z.object({ id: z.string(), role: z.string(), sessionID: z.string() }).passthrough(),
        parts: z.array(z.unknown()) }).passthrough()).parse(messages)
      const candidates = parsed.filter((message) => message.info.role === "assistant" && message.info.sessionID === task.plan.session)
        .flatMap((message) => message.parts.map((item) => this.matches(item, task))
          .filter((item) => item && item.messageID === message.info.id))
        .filter((item) => item && (!binding?.part_id || item.id === binding.part_id))
      const creations = candidates.filter((item) => item?.state.input.action === "create")
      const selected = creations.length ? creations : candidates
      if (selected.length !== 1) throw new Error("Cannot identify exactly one originating task card")
      const current = selected[0]!
      const next = { ...current, state: { ...current.state, metadata: { ...current.state.metadata,
        inspire_progress: { ...value, synced_at: Date.now() },
        // Separate metadata survives an older already-running observer updating
        // its progress snapshot; old publishers preserve unknown metadata keys.
        inspire_results: value.results, inspire_delivery: value.delivery, inspire_results_total: value.results_total,
        inspire_selection: value.selection, inspire_preparation_diagnostic: value.preparation_diagnostic } } }
      await this.request(task, `/message/${encodeURIComponent(current.messageID)}/part/${encodeURIComponent(current.id)}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) })
      this.store.db.query(`INSERT INTO presentation(task_id,message_id,part_id,fingerprint,retry_after,error) VALUES(?,?,?,?,0,NULL)
        ON CONFLICT(task_id) DO UPDATE SET message_id=excluded.message_id,part_id=excluded.part_id,
        fingerprint=excluded.fingerprint,retry_after=0,error=NULL`).run(task.id, current.messageID, current.id, fingerprint)
      return { delivered: true, part_id: current.id }
    } catch (error) {
      const reason = error instanceof Error ? error.message : "Progress delivery unavailable"
      this.store.db.query(`INSERT INTO presentation(task_id,message_id,part_id,retry_after,error) VALUES(?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET retry_after=excluded.retry_after,error=excluded.error`)
        .run(task.id, binding?.message_id ?? "", binding?.part_id ?? "", Date.now() + 60000, reason)
      return { delivered: false, reason }
    }
  }
}
