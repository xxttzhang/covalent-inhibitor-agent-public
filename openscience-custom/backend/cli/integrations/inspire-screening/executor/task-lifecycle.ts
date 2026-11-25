import { approve } from "./approval"
import { ensureDaemon } from "./host"
import type { Store } from "./store"

/** Shared native lifecycle used by the existing tool and workflow adapter.
 * Observers run only after the original approval/cancel/wake checks. */
export async function advanceNativeTask(store: Store, id: string, action: "approve" | "cancel" | "wake" | "status",
  context: Parameters<typeof approve>[2], observer: typeof ensureDaemon = ensureDaemon) {
  const task = store.get(id)
  if (task.plan.directory !== context.directory) throw Error("Task belongs to another project")
  if (action === "approve") {
    await approve(store, task.id, context)
    await observer(store)
  }
  if (action === "cancel") {
    store.cancel(task.id, context.directory)
    await observer(store)
  }
  if (action === "wake") {
    if (!task.approval) throw Error("Unapproved task cannot be resumed")
    await observer(store)
  }
  return store.get(id)
}
