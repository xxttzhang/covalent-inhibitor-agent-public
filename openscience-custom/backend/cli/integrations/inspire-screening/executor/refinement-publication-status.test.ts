import { expect, test } from "bun:test"
import { digest } from "./model"
import { refinementPublicationStatus } from "./refinement-publication-status"

const report = { workflow_id: "workflow-current", counts: { B2: 8 } }
const row = { workflow_id: report.workflow_id, state: "COMPLETE", report_digest: digest(report) }

test("completed exact report overrides stale unpublished note without changing scientific evidence", () => {
  const before = digest(report)
  const stale = { delivery_note: "尚未发布" }
  const result = { ...stale, ...refinementPublicationStatus(report, [row]) }
  expect(result.platform_files_published).toBe(true)
  expect(result.delivery_note).toContain("交付记录已完成")
  expect(result.delivery_note).not.toContain("尚未发布")
  expect(digest(report)).toBe(before)
})

test("missing, partial, other-workflow and outdated-report deliveries cannot claim publication", () => {
  for (const rows of [[], [{ ...row, state: "PAUSED" }], [{ ...row, state: "WAITING" }],
    [{ ...row, workflow_id: "older" }], [{ ...row, report_digest: "older" }]]) {
    const result = refinementPublicationStatus(report, rows)
    expect(result.platform_files_published).toBe(false)
    expect(result.delivery_note).toContain("尚无匹配")
  }
  expect(refinementPublicationStatus(null, [row]).platform_files_published).toBe(false)
})
