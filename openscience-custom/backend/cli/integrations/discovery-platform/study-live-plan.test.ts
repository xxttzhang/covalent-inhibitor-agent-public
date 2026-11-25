import { test, expect } from "bun:test"
import path from "node:path"
import { study } from "@synsci/util/discovery-study"
import { compileLiveLibrary, readLibraryPlan } from "./study-live-plan"
import { studyActionResult } from "./study-actions"

test("online compilation requires host authorization and rejects historical reuse before resource queries", async () => {
  const root = path.resolve(import.meta.dir, "../../../..")
  const plan = study.parse(await Bun.file(path.join(root,
    ".discovery/studies/study-e58f69ec-8a34-45dc-b8cc-900049265515/plan.json")).json())
  expect(await readLibraryPlan(root, plan)).toBeNull()
  await expect(studyActionResult(root, plan.project, { action: "compile-library", study_id: plan.id }))
    .rejects.toThrow("Host remote-read authorization")
  await expect(compileLiveLibrary(root, plan, async () => { throw Error("Untrusted project") }))
    .rejects.toThrow("Untrusted project")
  await expect(compileLiveLibrary(root, plan, async () => {})).rejects.toThrow("historical reuse")
  expect(await readLibraryPlan(root, plan)).toBeNull()
})
