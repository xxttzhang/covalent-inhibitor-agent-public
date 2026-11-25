import { open, ensureDelivery } from "./host"
import { runRefinements } from "./refinement-execution"

const store = await open()
const key = "refinement-compute"
const claimed = store.db.transaction(() => {
  const row = store.db.query("SELECT pid,heartbeat FROM daemon WHERE key=?").get(key) as { pid: number; heartbeat: number } | null
  if (row) {
    try { process.kill(row.pid, 0); return false } catch { /* Missing owner. */ }
  }
  store.db.query("INSERT INTO daemon(key,pid,heartbeat) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET pid=excluded.pid,heartbeat=excluded.heartbeat")
    .run(key, process.pid, Date.now())
  return true
})()
if (!claimed) { store.close(); process.exit(0) }
const heartbeat = setInterval(() => store.db.query("UPDATE daemon SET heartbeat=? WHERE key=? AND pid=?").run(Date.now(), key, process.pid), 10000)
const abort = new AbortController()
process.on("SIGTERM", () => abort.abort())
process.on("SIGINT", () => abort.abort())
try {
  while (!abort.signal.aborted) {
    try { await runRefinements(store, abort.signal) } catch { /* Keep exact runs for subsequent reconciliation. */ }
    await ensureDelivery(store).catch(() => undefined)
    if (!abort.signal.aborted) await Bun.sleep(15000)
  }
} finally {
  clearInterval(heartbeat)
  store.db.query("DELETE FROM daemon WHERE key=? AND pid=?").run(key, process.pid)
  store.close()
}
