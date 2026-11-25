import path from "node:path"
import { mkdir, chmod } from "node:fs/promises"
import { startWorker } from "./supervisor"
import z from "zod"
import { connection } from "./inspire"
import { planSchema } from "./model"
import { Store } from "./store"

// Installation-owned configuration and task state can live outside the source
// checkout. Never take this boundary from model or project-authored input.
const runtime = process.env.COVALENT_RUNTIME_ROOT
if (runtime && !path.isAbsolute(runtime)) throw Error("COVALENT_RUNTIME_ROOT must be absolute")
export const root = runtime ?? path.resolve(import.meta.dir, "../../../../../..")
export const directory = path.join(root, ".cache/inspire-executor")
export const filename = path.join(directory, "tasks.sqlite")
const spec = z.object({ kind: z.enum(["hpc", "job"]), workspace: z.string(), project: z.string(), group: z.string(),
  quota: z.string(), image: z.string(), image_type: z.enum(["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]), priority: z.number().int() }).strict()
export async function settings() {
  return z.object({ connection, runtime: planSchema.shape.runtime, cpu: spec, gpu: spec }).strict()
    .parse(await Bun.file(path.join(root, "configs/inspire-executor.json")).json())
}
export async function open() {
  await mkdir(directory, { recursive: true, mode: 0o700 })
  await chmod(directory, 0o700)
  const store = new Store(filename)
  await chmod(filename, 0o600)
  return store
}
export async function ensureDelivery(store: Store) {
  // Old approvals do not authorize the new automatic storage policy. Starting
  // its observer is safe only for a policy explicitly included in an approval.
  if (store.list().some((task) => task.plan.delivery && task.approval)) {
    await startWorker(store, "delivery", path.join(import.meta.dir, "delivery-daemon.ts"))
  }
  if (store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get() &&
    store.db.query("SELECT id FROM refinement_workflows WHERE state='APPROVED' AND json_extract(plan,'$.delivery.mode')='all-refinement-artifacts-v1' LIMIT 1").get()) {
    await startWorker(store, "refinement-delivery", path.join(import.meta.dir, "refinement-delivery-daemon.ts"))
  }
}
export async function ensureDaemon(store: Store) {
  // Sequential confirmed startup also avoids competing schema initialization.
  await ensureDelivery(store)
  if (store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get() &&
    store.db.query("SELECT id FROM refinement_workflows WHERE state='APPROVED' AND json_extract(plan,'$.stages[0].runtime_admission')='cpu-before-approval-gpu-at-start-v1' LIMIT 1").get()) {
    await startWorker(store, "refinement-compute", path.join(import.meta.dir, "refinement-daemon.ts"))
  }
  await startWorker(store, "main", path.join(import.meta.dir, "daemon.ts"))
}
