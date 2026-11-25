import path from "node:path"
import { open, ensureDaemon } from "./host"

// Gracefully replace only this executor's observer after source updates. This
// never restarts the platform server or stops/submits remote jobs directly.
const expected = Number(process.argv[2])
if (!Number.isSafeInteger(expected) || expected <= 1) throw new Error("Explicit expected observer PID required")
const key = process.argv.find((item) => item.startsWith("--worker="))?.slice(9) ?? "main"
const entries: Record<string, string> = { main: "daemon.ts", "refinement-compute": "refinement-daemon.ts", "refinement-delivery": "refinement-delivery-daemon.ts" }
const entry = entries[key]
if (!entry) throw new Error("Unsupported observer worker")
const store = await open()
try {
  const row = store.db.query("SELECT pid FROM daemon WHERE key=?").get(key) as { pid: number } | null
  if (row?.pid !== expected) throw new Error("Observer identity changed; inspect before replacing")
  const child = Bun.spawn(["/bin/ps", "-p", String(expected), "-o", "command="], { stdout: "pipe", stderr: "pipe" })
  const command = (await new Response(child.stdout).text()).trim()
  if (await child.exited !== 0 || !command.endsWith(` --no-env-file ${path.join(import.meta.dir, entry)}`)) {
    throw new Error("PID is not the expected screening observer")
  }
  process.kill(expected, "SIGTERM")
  const deadline = Date.now() + 140000
  for (;;) {
    const current = store.db.query("SELECT pid FROM daemon WHERE key=?").get(key) as { pid: number } | null
    if (!current) break
    if (current.pid !== expected) throw new Error("Another observer took ownership; do not replace it")
    if (Date.now() > deadline) throw new Error("Observer did not exit gracefully; no forced kill or duplicate observer")
    await Bun.sleep(1000)
  }
  // Approved task receipts and per-stage reservations are retained unchanged.
  await ensureDaemon(store)
  console.log(JSON.stringify({ replaced_observer: expected, worker: key, platform_restarted: false, approval_changed: false }))
} finally { store.close() }
