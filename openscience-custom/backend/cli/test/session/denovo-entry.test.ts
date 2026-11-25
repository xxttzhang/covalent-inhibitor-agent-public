import { describe, expect, test } from "bun:test"
import { denovoEntry, denovoRequest, denovoReply } from "../../src/session/denovo-entry"
import type { MessageV2 } from "../../src/session/message-v2"

const command = "针对 EGFR C797，固定丙烯酰胺，用 PocketXMol 从头生成 8 个候选分子。"
const parts = (text: string) => [{ id: "part_1", sessionID: "ses_1", messageID: "msg_1", type: "text", text }] as MessageV2.Part[]

describe("registered de novo command", () => {
  test("extracts bounded count and conservative defaults", () => {
    expect(denovoRequest(command)?.slots).toBe(8)
    expect(denovoRequest("EGFR C797，固定丙烯酰胺，从头生成候选分子")?.slots).toBe(32)
    expect(denovoRequest(command.replace("8 个", "96 个"))?.slots).toBe(96)
    // The legacy natural request must enter model planning, not the disabled create shortcut.
    expect(denovoEntry(parts(command), "research")).toBe(false)
  })
  test("never executes questions, quotations, other scopes or budgets", () => {
    for (const text of ["怎么实现用户一句话 de_novo？", "请问" + command, "不要" + command, '"' + command + '"', "> " + command, command + "请改用其他 warhead", command.replace("C797", "C797S"), command.replace("EGFR", "KRAS"), command.replace("8 个", "97 个"), command.replace("8 个", "0 个"), command.replace("8 个", "-1 个"), command.replace("8 个", "1.5 个"), command + " 预算0点券", command + " 再生成两批"])
      expect(denovoRequest(text)).toBeUndefined()
  })
  test("attachments and synthetic text cannot authorize compute", () => {
    expect(denovoEntry(parts(command).map((p) => ({ ...p, synthetic: true })), "research")).toBe(false)
    expect(denovoEntry([...parts(command), { type: "file" } as MessageV2.FilePart], "research")).toBe(false)
    expect(denovoEntry(parts(command), "plan")).toBe(false)
  })
  test("host reply preserves actual result without inventing success", () => {
    expect(denovoReply({ output: "NEEDS_REVIEW" })).toBe("NEEDS_REVIEW")
    expect(denovoReply(null)).not.toContain("成功")
  })
})

test("study commands route to frozen plan execution with no additional instructions", async () => {
  const { denovoStudyCommand } = await import("../../src/session/denovo-entry")
  const id = "study-0230aa0a-048e-45cc-9d4b-28dcf780d2b8"
  expect(denovoStudyCommand(`执行 de-novo 研究 ${id}`)).toEqual({ action: "create_study", study_id: id })
  expect(denovoEntry(parts(`执行 de-novo 研究 ${id}`), "research")).toBe(true)
  expect(denovoEntry(parts(`执行 de-novo 研究 ${id}`).map(p => ({ ...p, synthetic: true })), "research")).toBe(false)
  expect(denovoStudyCommand(`执行 de-novo 研究 ${id}，增加样本`)).toBeUndefined()
  expect(denovoStudyCommand(`“执行 de-novo 研究 ${id}”`)).toBeUndefined()
})
