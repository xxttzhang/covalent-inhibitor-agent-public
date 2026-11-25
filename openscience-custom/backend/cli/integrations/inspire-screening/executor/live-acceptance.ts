// Actual platform-chat acceptance. Never submits a workload outside the host.
import { writeFile } from "node:fs/promises"
import { open } from "./host"
import { digest, planSchema } from "./model"
import { deliveryStatus } from "./delivery-queue"

const root = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!root?.startsWith("/")) throw Error("Set an absolute COVSCREEN_ACCEPTANCE_ROOT for this historical acceptance")
const directory = `${root}/openscience-custom`
const attempt = process.argv.find((arg) => arg.startsWith("--attempt="))?.slice("--attempt=".length)
if (attempt !== undefined && !/^[a-z0-9-]{1,40}$/.test(attempt)) throw new Error("Invalid acceptance attempt label")
const receipt = `${root}/reports/inspire-test100-live-acceptance${attempt ? "-" + attempt : ""}.json`
const request = "针对 EGFR C797，使用 CovInDB 虚拟筛选库，100候选测试，点券不设上限。"
const suffix = "?directory=" + encodeURIComponent(directory)
async function api(route: string, body?: unknown) {
  const response = await fetch("http://127.0.0.1:4096" + route + suffix, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`Platform ${route}: HTTP ${response.status}`)
  const text = await response.text()
  return text ? JSON.parse(text) : null
}

if (process.argv.includes("--start")) {
  const active = await open()
  try {
    if (active.list().some((task) => task.plan.automation?.selected_mode === "test100" &&
      ["DRAFT", "APPROVED", "ACTIVE", "CANCEL_REQUESTED", "NEEDS_REVIEW"].includes(task.state)))
      throw new Error("Another test100 task is unresolved; do not create a duplicate")
  } finally { active.close() }
  await writeFile(receipt, JSON.stringify({ started: new Date().toISOString(), request }), { flag: "wx", mode: 0o600 })
  const session = await api("/session", { title: "[真实验收] EGFR C797 · 100候选 · 平台自主执行", permission: [
    { permission: "*", pattern: "*", action: "deny" },
    { permission: "inspire_screening_task", pattern: "*", action: "allow" },
    { permission: "remote_compute", pattern: "*", action: "ask" },
  ] })
  await Bun.write(receipt, JSON.stringify({ started: new Date().toISOString(), request, directory,
    session: session.id, project: session.projectID, approved: false }, null, 2))
  console.log(JSON.stringify({ session: session.id, project: session.projectID }))
  await api(`/session/${session.id}/prompt_async`, { agent: "research", effort: "normal",
    model: { providerID: "boyue-medicine", modelID: "deepseek-v4-pro" }, parts: [{ type: "text", text: request }] })
  process.exit(0)
}

const saved = await Bun.file(receipt).json()
if (!saved.session) throw new Error("Missing session receipt; do not repeat creation")
const store = await open()
try {
  const tasks = store.list().filter((task) => task.plan.session === saved.session)
  const permissions = (await api("/permission")).filter((item: { sessionID: string }) => item.sessionID === saved.session)
  const questions = (await api("/question")).filter((item: { sessionID: string }) => item.sessionID === saved.session)
  const messages = await api(`/session/${saved.session}/message`)
  if (process.argv.includes("--snapshot")) {
    const snapshot = { checked_at: new Date().toISOString(), session: saved.session, project: saved.project,
      pending_permissions: permissions.length, pending_questions: questions.length,
      tasks: tasks.map((task) => ({ id: task.id, state: task.state, approved: !!task.approval, error: task.error,
        stages: store.stages(task.id).map((stage) => ({ ...stage, manifest: stage.manifest ? JSON.parse(stage.manifest) : null })),
        delivery: deliveryStatus(store, task), events: store.events(task.id).slice(-8) })),
      daemon: store.db.query("SELECT key,pid,heartbeat FROM daemon").all(),
      assistant_completed: messages.some((item: { info: { role: string; time: { completed?: number } } }) => item.info.role === "assistant" && item.info.time.completed),
    }
    await Bun.write(receipt.replace(".json", "-status.json"), JSON.stringify(snapshot, null, 2))
    console.log(JSON.stringify(snapshot, null, 2))
    process.exit(0)
  }
  if (process.argv.includes("--approve-once")) {
    if (saved.approval_attempted || questions.length || tasks.length !== 1 || permissions.length !== 1)
      throw new Error("Ambiguous or previously attempted approval; inspect without replay")
    const task = tasks[0]
    const plan = planSchema.parse(task.plan)
    const permission = permissions[0]
    const compute = permission.metadata?.compute
    if (task.state !== "DRAFT" || task.approval || task.digest !== digest(plan) || plan.directory !== directory ||
      plan.request !== request || plan.automation?.selected_mode !== "test100" || plan.max_points !== null ||
      plan.shortlist !== 100 || plan.max_records !== 100000 || !plan.origin || !plan.delivery ||
      permission.permission !== "remote_compute" || permission.patterns.length !== 1 || permission.patterns[0] !== task.digest ||
      compute?.task_id !== task.id || compute?.digest !== task.digest || compute?.budget_policy !== "uncapped-test100-v1" ||
      plan.steps.map((step) => step.minutes).join(",") !== "60,120,60,10" ||
      plan.steps.some((step) => step.resource.quota !== (step.resource.kind === "job" ? "1,10,200" : "0,4,16")))
      throw new Error("Native approval differs from scoped acceptance")
    await Bun.write(receipt, JSON.stringify({ ...saved, task: task.id, digest: task.digest,
      approval_attempted: new Date().toISOString(), permission: permission.id, frozen_plan: plan }, null, 2))
    await api(`/permission/${permission.id}/reply`, { reply: "once" })
    console.log(JSON.stringify({ native_approval_replied: "once", task: task.id }))
  }
  console.log(JSON.stringify({ session: saved.session, project: saved.project,
    permissions: permissions.map((item: { id: string; permission: string; metadata: unknown }) => ({ id: item.id, permission: item.permission, metadata: item.metadata })),
    questions: questions.length,
    tasks: tasks.map((task) => ({ id: task.id, state: task.state, approval: !!task.approval, error: task.error,
      stages: store.stages(task.id), events: store.events(task.id).slice(-8) })),
    daemon: store.db.query("SELECT * FROM daemon").all(),
    messages: messages.map((item: { info: { role: string; time: unknown }; parts: { type: string; text?: string; tool?: string; state?: unknown }[] }) => ({ role: item.info.role, time: item.info.time,
      parts: item.parts.filter((part) => part.type === "text" || part.type === "tool").map((part) => ({ type: part.type, text: part.text, tool: part.tool, state: part.state })) })),
  }, null, 2))
} finally { store.close() }
