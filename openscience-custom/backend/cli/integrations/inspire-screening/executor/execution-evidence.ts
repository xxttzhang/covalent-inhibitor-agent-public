import { verifyOutcome, type Outcome } from "./model"
import type { Store } from "./store"

/** Local ledger evidence shared by route and research-loop adapters. A fresh
 * remote observation and scientific assessment remain separate operations. */
export function verifiedExecution(store: Store, id: string) {
  const task = store.get(id)
  const saved = store.stages(task.id)
  if (saved.length !== task.plan.steps.length || saved.some((stage, index) => stage.task_id !== task.id || stage.ordinal !== index ||
    stage.phase !== task.plan.steps[index]?.phase || stage.name !== `${task.id}-${stage.phase}`))
    throw Error("Native task stage identity changed")
  const verified: Outcome[] = []
  const stages = saved.map(stage => {
    if (!stage.manifest) {
      if (stage.state === "SUCCEEDED") throw Error("Successful native stage has no verified manifest")
      return { ...stage, manifest: null }
    }
    if (stage.state !== "SUCCEEDED" || verified.length !== stage.ordinal)
      throw Error("Native evidence is not an ordered verified stage prefix")
    const manifest = verifyOutcome(JSON.parse(stage.manifest), task, stage, verified.at(-1) ?? null)
    verified.push(manifest)
    return { ...stage, manifest }
  })
  if (task.state === "SUCCEEDED" && verified.length !== task.plan.steps.length)
    throw Error("Native task success lacks complete stage evidence")
  const approval = task.approval ? JSON.parse(task.approval) as { digest?: string; session?: string; boundary?: string } : null
  if (approval && (approval.digest !== task.digest || approval.session !== task.plan.session || approval.boundary !== "remote_compute"))
    throw Error("Native task approval belongs to another plan or conversation")
  if (!approval && (!["DRAFT", "CANCELLED"].includes(task.state) || saved.some(stage => stage.submitted !== null || stage.handle !== null || stage.manifest !== null)))
    throw Error("Native task has execution state without its exact-plan approval")
  return { task, stages }
}
