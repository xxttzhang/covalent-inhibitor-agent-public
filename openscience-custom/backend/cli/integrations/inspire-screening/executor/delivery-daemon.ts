import { open } from "./host"
import { deliverCompleted } from "./delivery-observer"

// Separate from the compute observer so a plugin update can start delivery for
// newly approved policies without interrupting an existing job observer.
const store = await open()
const claimed = store.db.transaction(() => {
  const row = store.db.query("SELECT pid,heartbeat FROM daemon WHERE key='delivery'").get() as { pid: number; heartbeat: number } | null
  if (row) {
    try { process.kill(row.pid, 0); return false } catch { /* Dead owner. */ }
  }
  store.db.query("INSERT INTO daemon(key,pid,heartbeat) VALUES('delivery',?,?) ON CONFLICT(key) DO UPDATE SET pid=excluded.pid,heartbeat=excluded.heartbeat")
    .run(process.pid, Date.now())
  return true
})()
if (!claimed) { store.close(); process.exit(0) }
const heartbeat = setInterval(() => {
  store.db.query("UPDATE daemon SET heartbeat=? WHERE key='delivery' AND pid=?").run(Date.now(), process.pid)
}, 10000)
const abort = new AbortController()
process.on("SIGTERM", () => abort.abort())
process.on("SIGINT", () => abort.abort())
try {
  while (!abort.signal.aborted) {
    const owner = store.db.query("SELECT pid FROM daemon WHERE key='delivery'").get() as { pid: number } | null
    if (owner?.pid !== process.pid) break
    const worked = await deliverCompleted(store, abort.signal)
    if (!abort.signal.aborted) await Bun.sleep(worked ? 1000 : 15000)
  }
} finally {
  clearInterval(heartbeat)
  store.db.query("DELETE FROM daemon WHERE key='delivery' AND pid=?").run(process.pid)
  store.close()
}
