import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises"
import { armGenerationRetry, inspectGenerationRetry, jobNotFound, quotaRejection } from "../../src/tool/denovo-generation-retry"

const task = "denovo-0123456789abcdef01234567"
const createError = { error: "Inspire command failed: job create " + JSON.stringify({ success: false,
  error: { type: "APIError", code: 13, message: "项目配额不足：该任务提交失败" } }) }
const missing = { success: false, error: { type: "JobNotFound", code: 16, message: `No web job matching '${task}' found.` } }

test("generation retry accepts only explicit quota rejection and matching JobNotFound", () => {
  expect(quotaRejection(createError).error.code).toBe(13)
  expect(jobNotFound(missing, task).error.code).toBe(16)
  expect(() => quotaRejection({ error: createError.error.replace("项目配额不足", "network error") })).toThrow("project-quota")
  expect(() => jobNotFound({ ...missing, error: { ...missing.error, message: "another task" } }, task)).toThrow("another task")
})

test("generation retry archives attempt one and replay cannot arm a different retry", async () => {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), "denovo-generation-retry-")))
  try {
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id: task, state: "NEEDS_REVIEW" }))
    await Bun.write(path.join(root, "submission.json"), JSON.stringify({ name: task, state: "SUBMITTING" }))
    await Bun.write(path.join(root, "submission_error.json"), JSON.stringify(createError))
    const receipt = await inspectGenerationRetry(root, task, async () => missing)
    expect((await armGenerationRetry(root, receipt)).already_armed).toBe(false)
    expect(JSON.parse(await readFile(path.join(root, "status.json"), "utf8"))).toMatchObject({ state: "PREPARING", retry_attempt: 2 })
    expect(JSON.parse(await readFile(path.join(root, "recovery-attempts/attempt-1/original-status.json"), "utf8"))).toMatchObject({ state: "NEEDS_REVIEW" })
    expect((await armGenerationRetry(root, receipt)).already_armed).toBe(true)
    await expect(armGenerationRetry(root, { ...receipt, reason: "changed" })).rejects.toThrow("different evidence")
  } finally { await rm(root, { recursive: true, force: true }) }
})
