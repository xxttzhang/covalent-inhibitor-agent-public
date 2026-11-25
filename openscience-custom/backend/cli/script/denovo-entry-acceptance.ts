// Real platform acceptance; --run explicitly enables the bounded fresh generation.
import path from "node:path"
const workspace = path.resolve(import.meta.dir, "../../../..")
const directory = path.join(workspace, "openscience-custom")
const base = "http://127.0.0.1:4096"
const run = process.argv.includes("--run")
const suffix = "?directory=" + encodeURIComponent(directory)
async function api(route: string, body?: unknown) {
  const response = await fetch(base + route + suffix, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  if (!response.ok) throw new Error(`${route}: ${response.status} ${await response.text()}`)
  if (response.status === 204) return
  return response.json()
}
const session = await api("/session", { title: run ? "一句话 de novo · EGFR C797 · 8 个候选" : "de novo 权限边界验收（禁止计算）", permission: [
  { permission: "*", pattern: "*", action: "deny" },
  { permission: "denovo_generate", pattern: "*", action: "allow" },
  { permission: "remote_compute", pattern: "*", action: run ? "ask" : "deny" },
] })
const filename = path.join(workspace, "reports", run ? "denovo-entry-live-20260929.json" : "denovo-entry-denied-20260929.json")
const text = "针对 EGFR C797，固定丙烯酰胺，用 PocketXMol 从头生成 8 个候选分子。"
const receipt = { session_id: session.id, directory, text, run, started: new Date().toISOString(), url: base + "/" + Buffer.from(directory).toString("base64url") + "/session/" + session.id }
await Bun.write(filename, JSON.stringify(receipt, null, 2))
console.log(JSON.stringify(receipt))
await api(`/session/${session.id}/prompt_async`, { agent: "research", effort: "normal", model: { providerID: "boyue-medicine", modelID: "deepseek-v4-pro" }, parts: [{ type: "text", text }] })
const deadline = Date.now() + (run ? 2700000 : 120000)
while (Date.now() < deadline) {
  const messages = await api(`/session/${session.id}/message`)
  const assistant = messages.findLast((m: { info: { role: string; time: { completed?: number } } }) => m.info.role === "assistant" && m.info.time.completed)
  if (assistant) {
    const calls = assistant.parts.filter((p: { type: string }) => p.type === "tool")
    const result = { ...receipt, completed: new Date().toISOString(), assistant, questions: await api("/question"), permissions: await api("/permission") }
    await Bun.write(filename, JSON.stringify(result, null, 2))
    console.log(JSON.stringify({ session: session.id, calls: calls.map((p: { tool: string; state: unknown }) => ({ tool: p.tool, state: p.state })), tokens: assistant.info.tokens }))
    if (calls.length !== 1 || calls[0].tool !== "denovo_generate" || assistant.info.tokens.input || assistant.info.tokens.output) throw new Error("Command was not deterministically routed exactly once")
    if (run && (calls[0].state.status !== "completed" || calls[0].state.metadata?.state !== "COMPLETED")) throw new Error("Fresh end-to-end generation did not complete")
    if (!run && calls[0].state.status !== "error") throw new Error("Compute deny was not enforced")
    process.exit(0)
  }
  await Bun.sleep(2000)
}
throw new Error("Acceptance observation deadline reached; preserve task and inspect")
