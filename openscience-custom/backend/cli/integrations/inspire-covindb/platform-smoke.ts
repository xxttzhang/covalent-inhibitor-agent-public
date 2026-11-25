// Explicit acceptance test through the platform's model/tool execution path.
// Permission is restricted to the single read-only query tool in this test session.
import path from "node:path"

if (!process.argv.includes("--allow-model-transfer")) {
  throw new Error("User approval required: this test sends 3 database records to the configured external model gateway. After approval, pass --allow-model-transfer.")
}

const root = path.resolve(import.meta.dir, "../../../../..")
const base = "http://127.0.0.1:4096"
const tool = "inspire-covindb_covindb_query"
const created = await fetch(`${base}/session`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ title: "[接入验收] Inspire 数据库 EGFR C797 只读查询", permission: [
    { permission: "*", pattern: "*", action: "deny" },
    { permission: tool, pattern: "*", action: "allow" },
    { permission: "mcp", pattern: tool, action: "allow" },
  ] }),
})
if (!created.ok) throw new Error("Test session creation failed.")
const session = await created.json()
console.log(JSON.stringify({ sessionID: session.id, phase: "created" }))
const response = await fetch(`${base}/session/${session.id}/message`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ model: { providerID: "deepseek", modelID: "deepseek-v4-pro" },
    agent: "research", effort: "normal", parts: [{ type: "text", text:
      "这是远程数据库连接的只读验收。只调用一次 inspire-covindb_covindb_query，参数 target=EGFR、site=C797、limit=3。不要检索网页、读本地文件、委派或运行计算。根据工具真实结果，用100字以内报告匹配记录数、不同抑制剂数及前三个CI编号。工具失败就如实报告，不得凭记忆补结果。" }],
  }),
})
if (!response.ok) throw new Error(`Platform prompt failed: ${response.status}`)
const answer = await response.json()
const messages = await fetch(`${base}/session/${session.id}/message`).then((r) => r.json())
const calls = messages.flatMap((message: { parts: { type: string; tool?: string; state?: { status: string; input?: unknown; output?: string } }[] }) =>
  message.parts.filter((part) => part.type === "tool" && part.tool === tool))
const text = (answer.parts ?? []).filter((part: { type: string }) => part.type === "text").map((part: { text: string }) => part.text).join("\n")
const report = { sessionID: session.id, tested_utc: new Date().toISOString(), calls, answer: text }
await Bun.write(path.join(root, "reports/inspire-covindb-platform-acceptance.json"), JSON.stringify(report, null, 2) + "\n")
if (!calls.some((call: { state?: { status: string; output?: string } }) => call.state?.status === "completed" && call.state.output?.includes("total_matching_records"))) {
  throw new Error("Model did not complete the real database tool call; inspect test session.")
}
console.log(JSON.stringify({ sessionID: session.id, phase: "passed", answer: text }))
