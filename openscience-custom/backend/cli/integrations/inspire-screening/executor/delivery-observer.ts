import { DeliveryQueue } from "./delivery-queue"
import { Inspire } from "./inspire"
import { Publications } from "./publication"
import { Progress } from "./progress"
import type { Task } from "./model"
import type { Store } from "./store"

export async function deliverCompleted(store: Store, abort: AbortSignal,
  transport: (task: Task) => ConstructorParameters<typeof DeliveryQueue>[1] = (task) => new Inspire(task.plan.connection),
  publisher = new Publications(store), progress = new Progress(store)) {
  const activity = { worked: false }
  for (const task of store.list().filter((task) => task.plan.delivery && (task.state === "SUCCEEDED" ||
    (task.state === "FAILED" && task.plan.delivery.failure)))) {
    if (abort.aborted) break
    await new DeliveryQueue(store, transport(task), publisher).tick(task.id, abort)
      .then((result) => { activity.worked ||= result.worked }, () => undefined)
    await progress.publish(store.get(task.id)).catch(() => undefined)
  }
  return activity.worked
}
