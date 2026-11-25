import { expect, test } from "bun:test"
import { deliveryLabel, inspireProgress, progressLabels, progressStageLabel } from "./inspire-progress"

const value = { version: 1, task_id: "screen-" + "a".repeat(24), plan_digest: "b".repeat(64), state: "FAILED",
  target: "EGFR C797", library: "CovInDB/Virtual_Screening_Library.csv", error: "CarsiDock failed", synced_at: 1000,
  stages: ["prepare", "carsidock", "rtmscore", "report"].map((phase) => ({ phase, state: "WAITING", job: null,
    input_count: null, success_count: null, failed_count: null, error: null })), note: "Core only" }

test("progress presentation preserves unknown counts and rejects invalid task/phase records", () => {
  expect(inspireProgress(value)?.stages[0].input_count).toBeNull()
  expect(progressLabels[value.state]).toContain("失败")
  for (const invalid of [null, {}, { ...value, task_id: "other" }, { ...value, state: "completed" },
    { ...value, stages: [...value.stages].reverse() }, { ...value, stages: value.stages.slice(1) },
    { ...value, stages: value.stages.map((stage) => ({ ...stage, input_count: -1 })) }]) {
    expect(inspireProgress(invalid)).toBeUndefined()
  }
})

test("unstarted downstream phases do not imply continued execution after stop", () => {
  expect(progressStageLabel("FAILED", "WAITING")).toBe("未执行（任务已失败）")
  expect(progressStageLabel("CANCELLED", "WAITING")).toBe("未执行（任务已取消）")
  expect(progressStageLabel("NEEDS_REVIEW", "WAITING")).toBe("未执行（等待检查）")
  expect(progressStageLabel("CANCEL_REQUESTED", "WAITING")).toBe("未执行（等待取消确认）")
  expect(progressStageLabel("ACTIVE", "WAITING")).toBe("待执行")
  expect(progressStageLabel("APPROVED", "WAITING")).toBe("待执行")
  expect(progressStageLabel("FAILED", "SUCCEEDED")).toBe("已完成")
  expect(progressStageLabel("NEEDS_REVIEW", "RUNNING")).toBe("实际运行中")
  expect(progressStageLabel("ACTIVE", "RUNNING")).toBe("实际运行中")
  expect(progressStageLabel("ACTIVE", "QUEUED")).toBe("排队或创建环境中")
  expect(progressStageLabel("ACTIVE", "SUBMITTED")).toBe("已提交，等待状态确认")
  for (const state of ["QUEUED", "SUBMITTED"]) {
    expect(inspireProgress({ ...value, stages: value.stages.map((stage) => ({ ...stage, state })) })).toBeDefined()
  }
})

test("persisted Result links retain version hashes and reject malformed identities", () => {
  const result = { phase: "report", name: "candidates.csv", artifact_id: "art_abcd-1234", version_id: "ver_abcd-1234",
    project_id: "project", sha256: "a".repeat(64), bytes: 120, published_at: 1000 }
  const current = { ...value, state: "SUCCEEDED", results: [result] }
  expect(inspireProgress(current)?.results?.[0].version_id).toBe(result.version_id)
  for (const change of [{ artifact_id: "javascript:bad" }, { sha256: "wrong" }, { bytes: -1 }, { published_at: NaN }, { phase: "other" }]) {
    expect(inspireProgress({ ...current, results: [{ ...result, ...change }] })).toBeUndefined()
  }
  expect(inspireProgress(value)).toBeDefined() // Old snapshots have no Result records.
})

test("automatic delivery remains separate from successful computation and partial files cannot count as complete", () => {
  const delivery = { state: "RUNNING", total_files: 55, completed_files: 1, total_bytes: 100, error: null, updated: 1000 }
  const task = { ...value, state: "SUCCEEDED", error: null, results_total: 1, delivery }
  expect(inspireProgress(task)?.delivery?.state).toBe("RUNNING")
  expect(inspireProgress({ ...task, delivery: { ...delivery, state: "PAUSED", error: "Storage cap exceeded" } })?.delivery?.error).toContain("cap")
  for (const change of [{ state: "COMPLETE" }, { completed_files: 56 }, { completed_files: -1 }, { total_bytes: NaN },
    { state: "UNKNOWN" }, { updated: -1 }]) {
    expect(inspireProgress({ ...task, delivery: { ...delivery, ...change } })).toBeUndefined()
  }
  const complete = { ...task, delivery: { ...delivery, state: "COMPLETE", completed_files: 55 } }
  expect(inspireProgress(complete)?.delivery?.state).toBe("COMPLETE")
  expect(inspireProgress({ ...complete, state: "FAILED" })).toBeUndefined()
  expect(inspireProgress({ ...value, delivery: null })).toBeDefined()
})

test("failure evidence completion is explicitly scoped and never changes failed screening labels", () => {
  const delivery = { scope: "failure_evidence", state: "COMPLETE", total_files: 4, completed_files: 4,
    total_bytes: 400, error: null, updated: 1000 }
  const card = inspireProgress({ ...value, delivery })
  expect(card?.state).toBe("FAILED")
  expect(deliveryLabel(card?.delivery?.scope, "COMPLETE")).toContain("筛选仍失败")
  expect(deliveryLabel("failure_evidence", "WAITING")).toBe("等待失败证据核验")
  expect(inspireProgress({ ...value, delivery: { ...delivery, scope: undefined } })).toBeUndefined()
  expect(inspireProgress({ ...value, state: "SUCCEEDED", delivery })).toBeUndefined()
  expect(inspireProgress({ ...value, delivery: { ...delivery, completed_files: 3 } })).toBeUndefined()
  expect(inspireProgress({ ...value, delivery: { ...delivery, state: "PAUSED", total_files: null, completed_files: 0 } })).toBeDefined()
})
