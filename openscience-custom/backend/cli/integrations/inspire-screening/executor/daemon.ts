import { open, ensureDelivery } from "./host"
import { Inspire } from "./inspire"
import { Runner } from "./runner"
import { Progress } from "./progress"
import { diagnosePreparation, preparationSnapshot } from "./preparation-diagnostic"

const store = await open()
const claimed = store.db.transaction(() => {
  const row = store.db.query("SELECT pid,heartbeat FROM daemon WHERE key='main'").get() as { pid: number; heartbeat: number } | null
  if (row) {
    try { process.kill(row.pid, 0); return false }
    catch { /* No live process owns this heartbeat. */ }
  }
  store.db.query("INSERT INTO daemon(key,pid,heartbeat) VALUES('main',?,?) ON CONFLICT(key) DO UPDATE SET pid=excluded.pid,heartbeat=excluded.heartbeat")
    .run(process.pid, Date.now())
  return true
})()
if (!claimed) { store.close(); process.exit(0) }
const progress = new Progress(store)
const heartbeat = setInterval(() => {
  store.db.query("UPDATE daemon SET heartbeat=? WHERE key='main' AND pid=?").run(Date.now(), process.pid)
}, 10000)
const state = { stopping: false }
process.on("SIGTERM", () => { state.stopping = true })
process.on("SIGINT", () => { state.stopping = true })
try {
  while (!state.stopping) {
    await ensureDelivery(store).catch(() => undefined)
    try {
      for (const task of store.list().filter((task) => ["APPROVED", "ACTIVE", "NEEDS_REVIEW", "CANCEL_REQUESTED"].includes(task.state))) {
        // Configuration edits apply to new drafts, never redirect an approved task.
        const runner = new Runner(store, new Inspire(task.plan.connection))
        await runner.tick(task.id)
        if (state.stopping) break
      }
      // Terminal outcomes also need delivery. Failure to reach the local UI
      // never changes execution state or schedules another job.
      for (const task of store.list()) {
        if (state.stopping) break
        if (["FAILED", "NEEDS_REVIEW"].includes(task.state) && !preparationSnapshot(store, task))
          await diagnosePreparation(store, task.id).catch(() => undefined)
        await progress.publish(task).catch(() => undefined)
      }
    } catch {
      // Details are in per-task events; don't leak environment or credentials to logs.
    }
    if (!state.stopping) await Bun.sleep(15000)
  }
} finally {
  clearInterval(heartbeat)
  store.db.query("DELETE FROM daemon WHERE key='main' AND pid=?").run(process.pid)
  store.close()
}
