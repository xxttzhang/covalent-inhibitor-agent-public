import { spawn } from "node:child_process"
import type { Store } from "./store"

export function workerAlive(store: Store, key: string) {
  const row = store.db.query("SELECT pid,heartbeat FROM daemon WHERE key=?").get(key) as { pid: number; heartbeat: number } | null
  if (!row || Date.now() - row.heartbeat >= 60000) return false
  try { process.kill(row.pid, 0); return true } catch { return false }
}

// A spawn request is not evidence of a running observer. Wait for its actual
// heartbeat before returning success. The worker's SQLite claim is still the
// cross-process singleton lock; this function never dispatches compute.
export async function startWorker(store: Store, key: string, entry: string, args: string[] = []) {
  if (workerAlive(store, key)) return
  const retained = store.db.query("SELECT pid FROM daemon WHERE key=?").get(key) as { pid: number } | null
  if (retained) {
    const alive = (() => { try { process.kill(retained.pid, 0); return true } catch { return false } })()
    if (alive) throw new Error(`${key} observer heartbeat stale but process ${retained.pid} is alive; inspect without spawning a duplicate`)
  }
  const child = spawn(process.execPath, ["--no-env-file", entry, ...args], { detached: true, stdio: "ignore" })
  const failure = { value: "" }
  child.once("error", () => { failure.value = "process spawn failed" })
  child.once("exit", (code, signal) => { failure.value = `process exited (${code ?? signal}) before ready` })
  child.unref()
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    if (workerAlive(store, key)) return
    if (failure.value) throw new Error(`${key} observer: ${failure.value}`)
    await Bun.sleep(50)
  }
  throw new Error(`${key} observer heartbeat not confirmed; inspect process ${child.pid}, do not claim startup succeeded`)
}
