import path from "node:path"
import { open } from "./host"

// Only retired file observers from this exact checkout; never app servers,
// compute observers, remote jobs, or the current database-registered owner.
const store = await open()
const entries = [
  { key: "delivery", entry: path.join(import.meta.dir, "delivery-daemon.ts") },
  { key: "refinement-delivery", entry: path.join(import.meta.dir, "refinement-delivery-daemon.ts") },
]
async function processes() {
  const child = Bun.spawn(["/bin/ps", "-axo", "pid=,args="], { stdout: "pipe", stderr: "pipe" })
  const output = await new Response(child.stdout).text()
  if (await child.exited !== 0) throw new Error("Cannot verify process identities")
  return output.split("\n").flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\S+) --no-env-file (.+)$/)
    const entry = match && entries.find((item) => item.entry === match[3])
    return match && entry && path.basename(match[2]) === "bun"
      ? [{ pid: Number(match[1]), command: line.trim().replace(/^\d+\s+/, ""), ...entry }] : []
  })
}
try {
  const observed = await processes()
  for (const entry of entries) {
    const owner = store.db.query("SELECT pid FROM daemon WHERE key=?").get(entry.key) as { pid: number } | null
    if (!owner || !observed.some((item) => item.key === entry.key && item.pid === owner.pid)) throw new Error("Current owner is not independently verified")
  }
  const retired = observed.filter((item) => (store.db.query("SELECT pid FROM daemon WHERE key=?").get(item.key) as { pid: number }).pid !== item.pid)
  console.log(JSON.stringify({ retired_file_observers: retired.map(({ pid, key }) => ({ pid, key })), apply: process.argv.includes("--apply") }))
  if (process.argv.includes("--apply")) {
    for (const item of retired) {
      const fresh = (await processes()).find((row) => row.pid === item.pid)
      const owner = store.db.query("SELECT pid FROM daemon WHERE key=?").get(item.key) as { pid: number } | null
      if (!fresh || fresh.command !== item.command || !owner || owner.pid === item.pid) continue
      process.kill(item.pid, "SIGTERM")
    }
    console.log(JSON.stringify({ graceful_signals_sent: true, compute_touched: false, database_rows_deleted: false }))
  }
} finally { store.close() }
