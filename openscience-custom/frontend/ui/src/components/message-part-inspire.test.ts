import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"

test("Inspire approval shows scope and live point units independently of Modal", () => {
  const source = readFileSync(new URL("./message-part.tsx", import.meta.url), "utf8")
  const details = source.slice(source.indexOf("function computeDetails"), source.indexOf("function PermissionActions"))
  expect(details).toContain('plan.provider === "inspire"')
  // Field formatting moved to the tested helper; verify this renderer still
  // delegates to it instead of asserting its old inline implementation.
  expect(details).toContain("return inspireDetails(plan)")
  expect(source).toContain('compute()?.provider === "inspire"')
  expect(source.includes("审批 Inspire 筛选任务")).toBe(true)
})
