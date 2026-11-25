import { expect, test } from "bun:test"
import { mkdtemp } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { Store } from "./store"
import { startWorker, workerAlive } from "./supervisor"

test("a stale heartbeat with a live PID cannot launch a replacement", async () => {
  const store = new Store(":memory:")
  try {
    store.db.query("INSERT INTO daemon(key,pid,heartbeat) VALUES('fixture',?,?)").run(process.pid, Date.now() - 120000)
    expect(workerAlive(store, "fixture")).toBe(false)
    await expect(startWorker(store, "fixture", "/nonexistent/never-spawn.ts")).rejects.toThrow("alive")
    expect(store.db.query("SELECT pid FROM daemon WHERE key='fixture'").get()).toEqual({ pid: process.pid })
  } finally { store.close() }
})

test("observer startup confirms a real child heartbeat, is idempotent, and reports early exit", async () => {
  const folder = await mkdtemp(path.join(tmpdir(), "screening-supervisor-"))
  const filename = path.join(folder, "tasks.sqlite")
  const store = new Store(filename)
  const entry = path.join(folder, "observer.ts")
  await Bun.write(entry, `import {Database} from 'bun:sqlite';
    const db=new Database(process.argv[2]); db.exec('PRAGMA busy_timeout=5000');
    if(process.argv[3]==='fail') process.exit(3);
    const pulse=()=>db.query("INSERT INTO daemon(key,pid,heartbeat) VALUES('fixture',?,?) ON CONFLICT(key) DO UPDATE SET pid=excluded.pid,heartbeat=excluded.heartbeat").run(process.pid,Date.now());
    pulse(); setInterval(pulse,100);`)
  try {
    await expect(startWorker(store, "fixture", entry, [filename, "fail"])).rejects.toThrow("before ready")
    expect(workerAlive(store, "fixture")).toBe(false)
    await startWorker(store, "fixture", entry, [filename])
    expect(workerAlive(store, "fixture")).toBe(true)
    const original = store.db.query("SELECT pid FROM daemon WHERE key='fixture'").get() as { pid: number }
    await startWorker(store, "fixture", entry, [filename])
    expect(store.db.query("SELECT pid FROM daemon WHERE key='fixture'").get()).toEqual(original)
  } finally {
    const row = store.db.query("SELECT pid FROM daemon WHERE key='fixture'").get() as { pid: number } | null
    if (row) process.kill(row.pid, "SIGTERM")
    store.close()
  }
})
