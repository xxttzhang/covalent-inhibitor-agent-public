// Real chat entry acceptance. The test session DENIES compute; no job can be approved.
import path from "node:path"
import { open } from "./host"
const base = "http://127.0.0.1:4096"
const directory = path.resolve(import.meta.dir, "../../../..")
const suffix = "?directory=" + encodeURIComponent(directory)
async function api(route: string, body?: unknown) {
  const response = await fetch(base + route + suffix, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`Acceptance API ${route}: ${response.status}`)
  return response.json()
}
const session = await api("/session", { title: "[验收] 一句话筛选入口（禁止计算）", permission: [
  { permission: "*", pattern: "*", action: "deny" },
  { permission: "inspire_screening_task", pattern: "*", action: "allow" },
  { permission: "remote_compute", pattern: "*", action: "deny" },
] })
console.log(JSON.stringify({ session: session.id, submitted: false }))
const text = process.argv.includes("--preflight")
  ? process.argv.includes("--test100")
    ? "针对 EGFR C797，使用 CovInDB 虚拟筛选库，100候选测试，点券不设上限。"
    : "针对 EGFR C797，使用 CovInDB 虚拟筛选库，标准筛选，预算上限50点券。"
  : "针对 EGFR C797，使用 CovInDB 虚拟筛选库，启动筛选。"
await api(`/session/${session.id}/prompt_async`, { agent: "research", effort: "normal",
  model: { providerID: "boyue-medicine", modelID: "deepseek-v4-pro" }, parts: [{ type: "text", text }] })
const deadline = Date.now() + 120000
const seen = { question: false, completed: false }
try {
  while (Date.now() < deadline) {
    const questions = await api("/question")
    for (const question of questions.filter((item: { sessionID: string }) => item.sessionID === session.id)) {
      seen.question = true
      await api(`/question/${question.id}/reject`, {})
    }
    const messages = await api(`/session/${session.id}/message`)
    if (messages.some((item: { info: { role: string; time: { completed?: number } } }) => item.info.role === "assistant" && item.info.time.completed)) {
      seen.completed = true
      break
    }
    await Bun.sleep(500)
  }
} finally {
  if (!seen.completed) await api(`/session/${session.id}/abort`, {})
}
const messages = await api(`/session/${session.id}/message`)
const assistants = messages.filter((item: { info: { role: string } }) => item.info.role === "assistant")
const calls = assistants.flatMap((item: { parts: { type: string; tool?: string }[] }) => item.parts.filter((part) => part.type === "tool"))
const store = await open()
const tasks = store.list().filter((task) => task.plan.session === session.id).map((task) => ({
  id: task.id, state: task.state, approval: !!task.approval,
  mode: task.plan.automation?.selected_mode, max_points: task.plan.max_points, shortlist: task.plan.shortlist,
  max_records: task.plan.max_records, steps: task.plan.steps.map((step) => ({ phase: step.phase, minutes: step.minutes, quota: step.resource.quota })),
  approval_requested: store.events(task.id).some((event) => (event as { kind: string }).kind === "approval_requested"),
  submitted: store.stages(task.id).some((stage) => stage.submitted !== null || stage.handle !== null),
}))
store.close()
const report = { tested_utc: new Date().toISOString(), session: session.id, ...seen,
  tasks,
  calls: calls.map((call: { tool: string; state: { status: string; error?: string } }) => ({ tool: call.tool, state: call.state.status, error: call.state.error })),
  model_tokens: assistants.reduce((sum: number, item: { info: { tokens: { input: number; output: number } } }) => sum + item.info.tokens.input + item.info.tokens.output, 0),
  submitted: false, note: "Native compute permission denied in this test session; rejection is intentional." }
const filename = path.resolve(import.meta.dir, "../../../../../../reports", process.argv.includes("--test100") ? "inspire-test100-entry-acceptance.json" : process.argv.includes("--preflight") ? "inspire-entry-preflight-acceptance.json" : "inspire-entry-acceptance.json")
await Bun.write(filename, JSON.stringify(report, null, 2) + "\n")
console.log(JSON.stringify(report))
if (!seen.completed || calls.length !== 1 || calls[0].tool !== "inspire_screening_task" || report.model_tokens !== 0 ||
  (!process.argv.includes("--preflight") && (!seen.question || tasks.length)) ||
  (process.argv.includes("--preflight") && (tasks.length !== 1 || tasks[0].state !== "CANCELLED" ||
    !tasks[0].approval_requested || tasks[0].approval || tasks[0].submitted))) throw new Error("Deterministic entry acceptance failed")
if (process.argv.includes("--test100") && (tasks[0].mode !== "test100" || tasks[0].max_points !== null || tasks[0].shortlist !== 100))
  throw new Error("test100 plan differs from requested conditions")
