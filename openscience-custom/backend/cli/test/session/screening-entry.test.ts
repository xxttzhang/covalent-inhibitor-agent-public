import { expect, test } from "bun:test"
import { screeningEntry, screeningReply } from "../../src/session/screening-entry"
import { SessionProcessor } from "../../src/session/processor"
import { Session } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { Instance } from "../../src/project/instance"
import { Identifier } from "../../src/id/id"
import { Provider } from "../../src/provider/provider"
import { tmpdir } from "../fixture/fixture"
import { targetCommand } from "@synsci/util/screening-target"

const command = "针对 EGFR C797，使用 CovInDB 虚拟筛选库，标准筛选，预算上限50点券。"
const parts = (text: string): MessageV2.Part[] => [{ id: "prt_test", messageID: "msg_test", sessionID: "ses_test", type: "text", text }]

test("structured new-target commands select parameter intake, never quoted or negated instructions", () => {
  const text = targetCommand({ target: "BTK", receptor: "prepared.cif", chain: "B", residue: "CYS", residue_number: 481,
    insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" }, "standard", 50)
  expect(screeningEntry(parts(text), "research")).toBe(true)
  for (const prefix of ["不要", "请解释", "例如\n", "```\n", "> "])
    expect(screeningEntry(parts(prefix + text), "research")).toBe(false)
  expect(screeningEntry(parts(text + "\n不用启动"), "research")).toBe(false)
})

test("versioned research pilot command reaches the normal bound intake and approval path", () => {
  const text = targetCommand({ target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
    insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" }, "research_pilot_v1", 1)
  expect(screeningEntry(parts(text), "research")).toBe(true)
  expect(screeningEntry(parts("例如\n" + text), "research")).toBe(false)
  expect(screeningEntry(parts(text + "\n不要启动"), "research")).toBe(false)
})

test("only direct supported screening commands select the executor", () => {
  expect(screeningEntry(parts(command), "research")).toBe(true)
  expect(screeningEntry(parts(command), "plan")).toBe(false)
  for (const prefix of ["请问如何", "不要", "请解释", "例如", "之前", "请帮我实现", "先别", "只查询", "> ", "```\n"]) {
    expect(screeningEntry(parts(prefix + command), "research")).toBe(false)
  }
  for (const text of [command.replace("C797", "C797S"), command.replace("EGFR", "BTK"), command + "全库", command + "AF3精筛"]) {
    expect(screeningEntry(parts(text), "research")).toBe(false)
  }
  expect(screeningEntry(parts(command).map((part) => ({ ...part, synthetic: true })), "research")).toBe(false)
  expect(screeningReply({ output: "invalid" })).not.toContain("任务已登记")
})

test("host-dispatched tools persist native metadata and outcomes without an LLM stream", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const media = { text: true, audio: false, image: false, video: false, pdf: false }
    const model = Provider.Model.parse({ id: "not-called", providerID: "fixture", name: "No model call",
      api: { id: "not-called", npm: "unused" }, capabilities: { temperature: false, reasoning: false,
        attachment: false, toolcall: true, input: media, output: media, interleaved: false },
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } }, limit: { context: 1000, output: 100 },
      status: "active", options: {}, headers: {}, release_date: "2026-09-14" })
    const assistant: MessageV2.Assistant = { id: Identifier.ascending("message"), sessionID: session.id,
      role: "assistant", parentID: Identifier.ascending("message"), agent: "research", mode: "research",
      providerID: model.providerID, modelID: model.id, path: { cwd: tmp.path, root: tmp.path }, cost: 0,
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, time: { created: Date.now() } }
    await Session.updateMessage(assistant)
    const processor = SessionProcessor.create({ assistantMessage: assistant, sessionID: session.id, model,
      abort: new AbortController().signal })
    await processor.startHostTool("host-command", "inspire_screening_task", { action: "create" })
    processor.toolMetadata("host-command", { action: "create" }, { title: "等待审批", metadata: { submitted: false } })
    await expect(processor.executeTool("host-command", { action: "create" }, async () => {
      throw new Error("Approval rejected; no submission")
    })).rejects.toThrow("Approval rejected")
    const stored = await MessageV2.parts(assistant.id)
    expect(stored).toHaveLength(1)
    expect(stored[0]).toMatchObject({ type: "tool", tool: "inspire_screening_task",
      state: { status: "error", metadata: { submitted: false } } })
  } })
})
