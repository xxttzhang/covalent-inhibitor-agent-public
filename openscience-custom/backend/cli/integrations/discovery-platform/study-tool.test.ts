import { expect, test } from "bun:test"
import { studyTool } from "./study-tool"
import { studyAction } from "./study-actions"

test("agent study actions expose the backend contract including evidence recovery", () => {
  const native = studyTool("/unused", "schema-test", async () => {}, async () => false)
  expect([...native.args.action.options].sort()).toEqual(
    studyAction.options.map(option => option.shape.action.value).sort(),
  )
  const input = { action: "reuse-hypothesis", study_id: "study-00000000-0000-0000-0000-000000000000",
    source_study: "study-11111111-1111-1111-1111-111111111111",
    route: "de-novo", sha256: "a".repeat(64) } as const
  expect(studyAction.parse(input)).toEqual(input)
  expect(native.args.source_study.parse(input.source_study)).toBe(input.source_study)
  expect(native.args.sha256.parse(input.sha256)).toBe(input.sha256)
  expect(() => native.args.sha256.parse("unverified")).toThrow()
})
