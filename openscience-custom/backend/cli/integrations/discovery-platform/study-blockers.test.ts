import { test, expect } from "bun:test"
import path from "node:path"
import { hypothesisBlockers } from "@synsci/util/discovery-study-report"

test("actual missing-parent proposal exposes blockers without changing its state", async () => {
  const value = await Bun.file(path.resolve(import.meta.dir,
    "../../../../results/target-hypotheses/hyp-5bdd9ed71c934007bf0dcbd0/proposal-a796fce1ce89/hypothesis.json")).json()
  const before = JSON.stringify(value)
  const lines = hypothesisBlockers(value.handoff)
  expect(lines).toContain("缺少输入：parent_ligand")
  expect(lines).toContain("缺少输入：parent_evidence_ids")
  expect(lines).toContain(value.handoff.compatibility_blockers[0])
  expect(value.handoff.state).toBe("NEEDS_INPUTS")
  expect(JSON.stringify(value)).toBe(before)
  expect(hypothesisBlockers({ state: "UNSUPPORTED", intake: null, unresolved: [null, 0, { text: "not a message" }] })).toEqual([])
})
