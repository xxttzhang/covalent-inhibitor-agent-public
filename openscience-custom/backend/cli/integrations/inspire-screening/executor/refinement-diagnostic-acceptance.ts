// Real platform-chat environment diagnostic, never a direct engine or CLI run.
// Start once; observation timeouts never replay the message or create compute.
import path from "node:path"
import { open as exclusive } from "node:fs/promises"
import { Identifier } from "../../../src/id/id"
import { open, root } from "./host"
import { digest } from "./model"

const attempt = process.argv.find((arg) => arg.startsWith("--attempt="))?.slice("--attempt=".length)
if (attempt !== undefined && !/^[a-z0-9-]{1,40}$/.test(attempt)) throw new Error("Invalid diagnostic attempt label")
const planning = process.argv.includes("--planning")
const presentation = process.argv.includes("--presentation")
const reporting = process.argv.includes("--report")
const existingReport = process.argv.includes("--existing-report")
if (existingReport && !reporting) throw new Error("Existing report interpretation requires --report")
const admission = process.argv.includes("--admission")
const confirmation = process.argv.includes("--confirmation")
const recovery = process.argv.includes("--recovery")
const delivery = process.argv.includes("--delivery")
if ([planning, presentation, reporting, admission, confirmation, recovery, delivery].filter(Boolean).length > 1) throw new Error("Choose one acceptance mode")
const action = delivery ? "resume_refinement_delivery" : admission || confirmation || recovery ? "approve_refinement" : reporting ? "refinement_report" : presentation ? "refinement_status" : planning ? "plan_refinement" : "diagnose_refinement"
const filename = path.join(root, `reports/inspire-refinement-${recovery ? "covdock-recovery" : confirmation ? "confirmation" : admission ? "admission" : reporting ? "report" : presentation ? "presentation" : planning ? "planning" : "environment"}-entry-${recovery ? "2026-09-16" : "2026-09-15"}${attempt ? "-" + attempt : ""}.json`)
const taskID = "screen-9b56c32831224a76a3848d43"
const store = await open()
const task = store.get(taskID)
const directory = task.plan.directory
const suffix = "?directory=" + encodeURIComponent(directory)
function refinements() {
  const table = store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()
  const workflows = table ? store.db.query("SELECT id,state,digest,approval FROM refinement_workflows ORDER BY id").all() : []
  const runs = store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()
    ? store.db.query("SELECT id,state,submitted,handle FROM refinement_runs ORDER BY id").all() : []
  // Hash grants rather than printing approval receipts into the conversation.
  return { workflow_count: workflows.length, workflow_digest: digest(workflows), run_count: runs.length, run_digest: digest(runs) }
}
async function api(route: string, body?: unknown) {
  const response = await fetch("http://127.0.0.1:4096" + route + (route.includes("?") ? "&" + suffix.slice(1) : suffix), body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`Platform ${route}: HTTP ${response.status}`)
  const text = await response.text()
  return text ? JSON.parse(text) : null
}

try {
  if (task.state !== "SUCCEEDED" || !task.approval) throw new Error("Completed core identity missing")
  if (process.argv.includes("--start")) {
    if (await Bun.file(filename).exists()) throw new Error("Diagnostic chat attempt exists; inspect it rather than sending again")
    const status = await api("/session/status")
    if (status[task.plan.session]?.type && status[task.plan.session].type !== "idle") throw new Error("Existing core conversation is busy")
    const pending = [...await api("/permission"), ...await api("/question")]
    if (pending.some((value: { sessionID: string }) => value.sessionID === task.plan.session)) throw new Error("Existing conversation has a pending user decision")
    const tools = await api("/experimental/tool?provider=boyue-medicine&model=deepseek-v4-pro")
    if (!tools.find((tool: { id: string; parameters?: { properties?: { action?: { enum?: string[] } } } }) =>
      tool.id === "inspire_screening_task" && tool.parameters?.properties?.action?.enum?.includes(action))) {
      throw new Error("Platform diagnostic tool is not online")
    }
    const message = Identifier.ascending("message")
    const recoveryProfile = recovery ? await Bun.file(path.join(root, "configs/inspire-refinement.json")).json() : null
    const request = delivery
      ? `请只恢复任务 ${taskID} 已批准的文件交付：调用 inspire_screening_task 的 resume_refinement_delivery。工作流 refineflow-76378344dae197d0022d4549 的 AF3 和 CovDock 都已完成，文件交付因传输超时暂停；只恢复原始文件队列，禁止新建方案、审批或提交任何计算，禁止重跑主筛、AF3 或 CovDock。完成工具调用后如实说明当前交付进度，不把恢复请求当作全部发布完成。保留科学局限：两阶段固定结构质检通过不等于实验命中，AF3–CovDock 构象一致性仍未评估，原始门控为 B2/NA。不要用 bash 或其他工具代跑。`
      : recovery
      ? `请为任务 ${taskID} 生成新的 CovDock-only 恢复审批。当前配置源码版本 ${recoveryProfile.code.release_id}。上次 CovDock refine-95cef023ac8d2dac2aa9e240 的 Slurm 日志确认因 60 分钟时限终止；云端 SUCCEEDED 标签不是科学成功。8 个候选的 LigPrep 完成，但没有最终 CovDock 姿势/评分。原授权已过期，不能复用。已部署的修复将中间文件保存到共享盘，并在执行前记录启动清单；尚未用新版本完成实际对接。旧版帮助注明 leadopt 约 1–3 小时/分子，当前单子任务新提案按 180 分钟/候选加 60 分钟准备余量，即 8 个候选最多 1500 分钟；这不是完成保证，须展示实时价格对应的新预算并等待本人审批。必须复用原 AF3 refine-6a2bd7b8ae43cab0421d4237 的真实 8 个结果，不重跑主筛或 AF3，不改变候选及科学参数。现在调用 inspire_screening_task plan_refinement；仅当 execution_draft.recovery_of 正确关联旧流程、AF3 reused=true 且草案没有阻塞，再调用 approve_refinement 展示原生资源审批卡。使用当前新代码及 Schrödinger 2021-2 官方许可环境，不加载注入库。这条消息不是资源批准，禁止替用户确认、继承旧授权或使用其他工具代跑。工具报错或复用不正确时保留实际错误并停止，不反复重试，不退回双阶段重跑。获批后应由平台后台跟踪对接、质检、汇总及原会话 Results 发布。`
      : confirmation
      ? `请推进任务 ${taskID} 的真实 AF3 → Schrödinger CovDock 双阶段精筛：先调用 inspire_screening_task plan_refinement 刷新已过期草案，保持原 EGFR C797、CovInDB 100 候选主筛结果和既定不确定性/多样性选择策略，不扩库、不重跑主筛。使用当前已修正配置中的 Schrödinger 2021-2 及其既有许可证，不升级软件。如果新方案存在阻塞，报告实际错误并停止；若草案阻塞为空，紧接着调用同一工具 approve_refinement 完成实时准入检查并展示原生统一资源审批卡，等待用户本人确认。这条消息仅请求展示审批卡，不是资源批准，禁止替用户回答或绕过审批。卡片须说明精确候选及原始 ID、选择理由、两阶段资源/时限/预算、固定质检规则和失败停止规则。一次确认后由平台后台执行、核验并回传结果，不用 bash 或其他工具代跑，不把 AF3 置信度当亲和力，也不把已提交当已完成。`
      : admission
      ? `请验收任务 ${taskID} 最新已保存精筛草案的正式准入入口：仅调用 inspire_screening_task approve_refinement。若这是 recovery_of 恢复草案，必须保留 AF3 reused=true，只申请新的 CovDock 授权。上次回读锁问题已修复，现在重新核验；不要把尚未批准草案的 ready_for_approval=false 自行推断成许可证或质量门槛失败。若许可证或其他条件失败，只报告工具的实际错误，不新建方案、不重试、不绕过。若条件通过，请停在原生审批卡等待我亲自确认；这条消息不是资源批准，不要代我确认。不得重跑主筛或 AF3、直接运行引擎或使用其他工具代跑。`
      : existingReport
      ? `上一次最终说明因模型服务 300 秒无响应而结束，不是计算失败。现在仅使用 read 工具读取已发布报告及原始门控清单，并给出不超过 250 字的结论：${path.join(directory, "inspire-results/screen-9b56c32831224a76a3848d43/refinements/refineflow-76378344dae197d0022d4549/report/0fef0d3320c52aea1b0fcae60d6731da78b87c4536f91e8bfd9623a2778417bc-refinement-report.md")}；${path.join(directory, "inspire-results/screen-9b56c32831224a76a3848d43/refinements/refineflow-76378344dae197d0022d4549/covdock/1da73daebe275b34a5a69dd51fbbab8f1592b07b2e95ffeeba94692b2032facb-stage5_gate_manifest.json")}。必须区分 8 个候选的固定结构质检通过与 B2 证据缺失：AF3–CovDock 姿势一致性尚未评估，不是实验命中。不要新建方案、审批、提交、重跑或调用任何计算工具，也不要将本次文件解读说成新执行。原始结果已在本会话 Files/Results 发布，但新结构预览仍未正式部署，不能说完整界面验收通过。若文件不可读请如实说明。`
      : reporting
      ? `请仅调用 inspire_screening_task 的 refinement_report 读取任务 ${taskID} 的 AF3 和薛定谔 CovDock 实际完成情况与综合报告。若两阶段尚未实际完成，请如实说明缺少执行结果，不能把已完成的主筛报告或精筛计划当作精筛报告。不要生成新方案、批准或提交计算，不要重跑主筛，不使用其他工具代跑。`
      : presentation
      ? `请仅调用 inspire_screening_task refinement_status 读取任务 ${taskID} 已保存的精筛方案，并依据工具的 interpretation 更正上一次回复中的排名解释：说明索引 29、33 的两种排名方向，排名分位 0 的含义，引擎失败与质检拒绝的区别，以及是否只需一次统一审批。不重新生成方案、不提交或批准计算，不使用其他工具读取文件。`
      : planning
      ? `在已完成任务 ${taskID} 的 EGFR C797、CovInDB 100 候选主筛基础上，请平台自动提出不确定性与结构多样性驱动的 AF3 和薛定谔 CovDock 精筛方案，列出实际候选数、原始 ID 和选择理由、资源、点券与时限，以及尚未通过的运行条件。说明工具返回的固定结构质检数值规则、缺失测量的处理和科学局限，不把 AF3 置信度当作亲和力验证。若工具返回双阶段 execution_draft，请如实说明它的状态与剩余阻塞，不把草案当作已批准任务。现在只生成方案，不批准或提交精筛，不重跑主筛、不扩大候选库。`
      : `请仅对已完成任务 ${taskID} 调用 inspire_screening_task 的 diagnose_refinement，核查 AF3 和薛定谔 CovDock 环境并报告具体阻塞。不要创建或批准新任务，不要运行推理或对接，不要重跑主筛，也不要使用 bash 或其他工具代跑。`
    const receipt = { started: new Date().toISOString(), task_id: taskID, session: task.plan.session, directory,
      message_id: message, request, action, before: { core_digest: task.digest, approval: digest(task.approval),
        stages: digest(store.stages(taskID)), task_count: store.list().length, refinements: refinements() } }
    const file = await exclusive(filename, "wx", 0o600)
    await file.writeFile(JSON.stringify(receipt, null, 2) + "\n")
    await file.close()
    await api(`/session/${task.plan.session}/prompt_async`, { messageID: message, agent: "research", effort: "normal",
      model: { providerID: "boyue-medicine", modelID: "deepseek-v4-pro" }, parts: [{ type: "text", text: request }] })
    console.log(JSON.stringify({ chat_sent: true, session: task.plan.session, message_id: message, compute_approval_sent: false }))
  }
  const saved = await Bun.file(filename).json()
  const messages = await api(`/session/${saved.session}/message`)
  const assistants = messages.filter((item: { info: { role: string; parentID?: string } }) => item.info.role === "assistant" && item.info.parentID === saved.message_id)
  const calls = assistants.flatMap((item: { parts: { type: string; tool?: string; state?: { status: string; input?: unknown; output?: string; error?: string } }[] }) =>
    item.parts.filter((part) => part.type === "tool").map((part) => ({ tool: part.tool, ...part.state })))
  const current = store.get(taskID)
  const status = (await api("/session/status"))[saved.session] ?? { type: "idle" }
  const observation = { observed: new Date().toISOString(), session: saved.session, message_id: saved.message_id,
    status, calls,
    assistant_text: assistants.flatMap((item: { parts: { type: string; text?: string }[] }) =>
      item.parts.filter((part) => part.type === "text" && part.text).map((part) => part.text!)).join("\n\n"),
    assistant_complete: status.type === "idle" && !!assistants.at(-1)?.info.time.completed &&
      !assistants.at(-1)?.info.error && assistants.at(-1)?.info.finish !== "tool-calls" &&
      assistants.at(-1)?.parts.some((part: { type: string; text?: string }) => part.type === "text" && !!part.text?.trim()),
    assistant_terminal: status.type === "idle" && !!assistants.at(-1)?.info.time.completed,
    assistant_error: assistants.at(-1)?.info.error ?? null,
    unchanged_core: current.digest === saved.before.core_digest && digest(current.approval) === saved.before.approval && digest(store.stages(taskID)) === saved.before.stages,
    new_core_tasks: store.list().length - saved.before.task_count,
    refinements: refinements(),
    unchanged_refinements: saved.before.refinements ? digest(refinements()) === digest(saved.before.refinements) : null,
    pending_permissions: (await api("/permission")).filter((value: { sessionID: string }) => value.sessionID === saved.session).length,
    compute_approval_sent: false }
  await Bun.write(filename.replace(".json", "-status.json"), JSON.stringify(observation, null, 2) + "\n")
  console.log(JSON.stringify({ ...observation, calls: calls.map((call: { tool: string; status: string; input?: unknown; output?: string; error?: string }) => {
    const output = call.output ? (() => { try { return JSON.parse(call.output) } catch { return null } })() : null
    const diagnosis = output?.diagnosis
    return { tool: call.tool, status: call.status, input: call.input, error: call.error,
      refinement_report: output?.verification_started_at ? { report_ready: output.report_ready, verification: output.verification,
        reason: output.reason, stages: output.stages, platform_files_published: output.platform_files_published,
        failure: output.failure ? { failure_evidence_ready: output.failure.failure_evidence_ready,
          verification: output.failure.verification, reason: output.failure.reason, compute_restarted: output.failure.compute_restarted,
          scientific_quality_verified: output.failure.scientific_quality_verified, unavailable: output.failure.unavailable,
          termination: output.failure.termination,
          inventories: output.failure.inventories ?? output.failure.files?.map((file: { id: string; engine: string; files: unknown[]; missing: string[] }) =>
            ({ id: file.id, engine: file.engine, file_count: file.files.length, missing: file.missing })) } : null } : null,
      execution_draft: output?.execution_draft, diagnosis: diagnosis ? {
      state: diagnosis.state, ready_to_submit: diagnosis.ready_to_submit, recorded_at: diagnosis.recorded_at,
      assets_stable: diagnosis.result?.assets_stable, af3: diagnosis.result?.af3,
      covdock: diagnosis.result?.covdock, blockers: diagnosis.result?.blockers,
    } : null, proposal: output?.proposal ? { state: output.proposal.state, version: output.proposal.version,
      quality_policy: output.proposal.quality_policy,
      selected_count: output.proposal.selection.selected_count, proposed_points: output.proposal.proposed_points,
      stages: output.proposal.stages.map((stage: { engine: string; minutes: number; candidate_count: number }) => ({ engine: stage.engine,
        minutes: stage.minutes, candidate_count: stage.candidate_count })), blockers: output.proposal.blockers,
      ready_for_approval: output.proposal.ready_for_approval } : null, result_state: output?.state }
  }) }))
} finally { store.close() }
