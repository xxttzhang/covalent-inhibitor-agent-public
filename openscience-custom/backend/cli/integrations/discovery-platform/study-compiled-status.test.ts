import { test, expect } from "bun:test"
import path from "node:path"
import { studyActionResult } from "./study-actions"

test("real compiled study receipts update route status without claiming computation", async () => {
  const root = path.resolve(import.meta.dir, "../../../..")
  for (const [id, route, state] of [
    ["study-00d8e67f-169a-4453-9e47-07ded41a738f", "library-first", "plan-compiled"],
    ["study-4f1f035a-a0c9-45d0-93aa-d1e6142f3b0b", "de-novo", "local-plan-compiled"],
    ["study-c92778ff-f0a4-4743-bf95-ea403b8966ac", "ligand-first", "upstream-plan-bound"],
  ]) {
    const value = await studyActionResult(root, "prj_dbfd2a34da2e4c78977238fcf59d39ae", { action: "get", study_id: id })
    if (!("routes" in value)) throw Error("Missing overview")
    expect(value.routes.find(item => item.route === route)?.state).toBe(state)
    expect(value.routes.find(item => item.route === route)?.reason).toContain("未提交")
    expect(value.candidates).toEqual([])
    expect(value.newComputeSubmitted).toBe(false)
  }
})

test("real Ligand upstream receipt is idempotent through the formal action", async () => {
  const root = path.resolve(import.meta.dir, "../../../..")
  const action = { action: "bind-ligand-plan", study_id: "study-c92778ff-f0a4-4743-bf95-ea403b8966ac" }
  const first = await studyActionResult(root, "prj_dbfd2a34da2e4c78977238fcf59d39ae", action)
  const second = await studyActionResult(root, "prj_dbfd2a34da2e4c78977238fcf59d39ae", action)
  expect(second).toEqual(first)
  if (!first || !("evidence" in first)) throw Error("Missing upstream receipt")
  expect(first.evidence).toBe("upstream-compiler-snapshot")
  expect(first.submitted).toBe(false)
  expect(first.approved).toBe(false)
})
