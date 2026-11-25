import { expect, test } from "bun:test"
import { mkdtemp, mkdir, rm, symlink, realpath } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { deNovoBatchResume } from "../../integrations/discovery-platform/study-denovo-batch"

test("recovery observes frozen plans without claiming execution or repeating selection", async () => {
  const base = await realpath(await mkdtemp(path.join(tmpdir(), "denovo-resume-")))
  const id = "denovo-" + "a".repeat(24)
  const folder = path.join(base, id)
  await mkdir(folder)
  try {
    expect((await deNovoBatchResume(base, id)).next.action).toBe("select_batch")
    await Bun.write(path.join(folder, "batch-vs-plan.json"), JSON.stringify({schema: "denovo-batch-vs-plan-v1"}))
    const vs = await deNovoBatchResume(base, id)
    expect(vs.next.action).toBe("batch_vs_status")
    expect(vs.remote_execution_verified).toBe(false)
    await Bun.write(path.join(folder, "batch-lo-input.json"), JSON.stringify({schema: "denovo-batch-lo-input-v1", task_id: id}))
    expect((await deNovoBatchResume(base, id)).next.action).toBe("plan_batch_lo")
    await Bun.write(path.join(folder, "batch-lo-plan.json"), JSON.stringify({schema: "denovo-batch-lo-plan-v1", task_id: "foreign"}))
    await expect(deNovoBatchResume(base, id)).rejects.toThrow("another task")
    await rm(path.join(folder, "batch-lo-plan.json"))
    await symlink(path.join(folder, "batch-lo-input.json"), path.join(folder, "batch-glide-plan.json"))
    await expect(deNovoBatchResume(base, id)).rejects.toThrow("without symlinks")
    await rm(path.join(folder, "batch-glide-plan.json"))
    await Bun.write(path.join(folder, "batch-lo-plan.json"), JSON.stringify({schema: "denovo-batch-lo-plan-v1", task_id: id}))
    await Bun.write(path.join(folder, "batch-lo-retry-input.json"), JSON.stringify({schema: "denovo-batch-lo-input-v1", task_id: id}))
    expect((await deNovoBatchResume(base, id)).next.action).toBe("plan_batch_lo")
    await Bun.write(path.join(folder, "batch-lo-retry-plan.json"), JSON.stringify({schema: "denovo-batch-lo-plan-v1", task_id: id}))
    const retry = await deNovoBatchResume(base, id)
    expect(retry.local_artifact).toBe("batch-lo-retry-plan.json")
    expect(retry.next.action).toBe("batch_lo_status")
    expect(retry.remote_execution_verified).toBe(false)
  } finally {
    await rm(base, {recursive: true, force: true})
  }
})
