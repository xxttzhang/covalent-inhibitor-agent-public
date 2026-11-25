import z from "zod"
import path from "node:path"
import { createHash } from "node:crypto"
import { spawn } from "node:child_process"
import { closeSync, openSync } from "node:fs"
import { cp, mkdir, writeFile } from "node:fs/promises"
import { Tool } from "./tool"
import { Instance } from "../project/instance"
import { MessageV2 } from "../session/message-v2"
import { Session } from "../session"
import { registeredDeNovoSource } from "./denovo-source"
import { bindStudyDeNovoRequest } from "./denovo-study"
import { readStudy } from "../../integrations/discovery-platform/study-store"
import { compileDeNovoPlan, readDeNovoPlan } from "../../integrations/discovery-platform/study-denovo-plan"
import { deNovoWorkflow } from "../../integrations/discovery-platform/study-denovo-workflow"
import { deNovoTasks, deNovoStatus, readDeNovoTask } from "./denovo-task"
import { readDeNovoResults } from "./denovo-results"
import { acknowledgeDeNovoCompute, registerDeNovoCompute } from "../session/denovo-continuation-compute"
import { selectDeNovoBatch, prepareDeNovoBatch, registerDeNovoBatch, planDeNovoBatch, statusDeNovoBatch } from "./denovo-selection"
import { sessionStudy, bindStudySession, boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { prepareDeNovoIndependentInput, readDeNovoIndependentInput, registerDeNovoIndependentInput,
  syncDeNovoIndependentReview } from "../../integrations/discovery-platform/study-denovo-review"

import { warheadRegistry } from "@synsci/util/warhead-registry"
import { deNovoBranches, materializeDeNovoBranch, deNovoCapability } from "../../integrations/discovery-platform/study-denovo-branches"
import { readStudyGenerationProfile } from "../../integrations/discovery-platform/study-denovo-compiler"
import { armGenerationRetry, inspectGenerationRetry, observeMissingInspireJob } from "./denovo-generation-retry"

const tasks = deNovoTasks
const digest = (input: string | Uint8Array) => createHash("sha256").update(input).digest("hex")
const statusSchema = deNovoStatus

function launch(root: string, python: string) {
  const fd = openSync(path.join(root, "worker.log"), "a", 0o600)
  try {
    const process = spawn(python, [path.join(root, "worker.py"), root], { detached: true, stdio: ["ignore", fd, fd], env: { ...globalThis.process.env, PYTHONDONTWRITEBYTECODE: "1" } })
    process.on("error", (error) => { void Bun.write(path.join(root, "status.json"), JSON.stringify({ id: path.basename(root), state: "NEEDS_REVIEW", error: error.message })) })
    process.unref()
  } finally { closeSync(fd) }
}

function report(status: z.infer<typeof statusSchema>) {
  if (status.state !== "COMPLETED" || !status.summary) return `任务 ${status.id}：${status.state}。${status.error ? "\n\n" + status.error : "\n\n后台任务继续执行；可通过 denovo_generate 的 status 查询。"}`
  const summary = status.summary
  const links = Object.entries(status.artifacts ?? {}).map(([name, item]) => `- [${name}](${item.source})`).join("\n")
  return `本次 PocketXMol 已生成 ${summary.requested_slots} 个候选槽位：**原始结构通过 ${summary.raw.pass_count} 个，约束优化后通过 ${summary.relaxed.pass_count} 个**。两组分别统计，不能相加。\n\n方案：EGFR WT C797（4G5J A），固定丙烯酰胺、仅从 N 生长。已完成结构与来源一致性检查；这些是结构候选，尚未证明抑制活性或形成共价键。\n\n任务：${status.id}。文件已保存到 Results：\n\n${links}`
}

export const DeNovoTool = Tool.define("denovo_generate", {
  description: "Before discovery_study.create, call registered_profile and use its exact pocket ID if target/site/receptor match the user request. FIRST RELEASE DEFAULT: PocketXMol only, fixed EGFR Cys797 and acrylamide; no CovaGEN or MD. After generation use select_batch → prepare_batch → register_batch → plan_batch_vs, then denovo_submit_batch_vs with native approval. Observe batch_vs_status until completed, read_batch_vs_products → prepare_batch_lo → plan_batch_lo → denovo_submit_batch_lo; observe batch_lo_status then evaluate_batch_lo. Next prepare_batch_prereaction → plan_batch_prereaction → denovo_submit_batch_prereaction (independent Glide SP with reference control); observe batch_prereaction_status, evaluate_batch_prereaction, report_batch. retry_study permits one separately approved generation resubmission only after an explicit project-quota rejection and a fresh exact JobNotFound observation; it preserves attempt one and rejects unknown outcomes. retry_batch_lo freezes one separately approved second attempt only after the exact verified pre-engine single-threaded-supervisor failure; old input, plan, approval and failed run stay intact. Then plan_batch_lo and denovo_submit_batch_lo. Each submission uses its frozen budget and exact task identity. Report unique molecules, prepared states and poses separately; keep failed and unmeasured candidates. Never interpret generation geometry as independent docking evidence or ranking as activity. workflow recovers local artifacts but requires observing the original remote request before any submission. Legacy single-candidate tools below are for existing studies only, not the default batch path. warheads lists research definitions, not execution support. Declare denovoBranches in discovery_study.create; branches reads their independent states and source-preserving candidates, branch_study idempotently creates a child study for one declared branch without compute. Each child needs its own verified hypothesis, frozen plan and native approval; unsupported chemistry stays WAITING_CAPABILITY. No bulk submission or shared raw-score ranking. prepare_evaluation freezes one verified native-study candidate plus an afatinib control for independent review, stripping generated coordinates; this prepares inputs only, requires host transport, and never submits docking or MD. evaluation reads that exact study source. register_evaluation transports only its hash-bound data to the active shared root; plan_evaluation compiles the shared CovDock job, evaluation_status/evaluate read back the same registered request into this study, and prepare_md_handoff preserves unmet scientific gates as blocked. replan freezes attempt 2 only after the original CovDock attempt is natively confirmed never dispatched and cancelled; it preserves the candidate, control and budget. Use denovo_cancel_evaluation for that exact cancellation. None of these actions submits a job; use the bound denovo_submit_evaluation entry with stage prereaction or adduct and separate native approval. The host supplies the exact frozen job arguments; raw submission tools remain hidden. evaluate measures receptor-aligned control RMSD and verifies the fixed terminal-acrylamide candidate reaction graph and specified stereochemistry from registered vendor output. plan_prereaction freezes a separate native-approval Vina job using coordinate-independent inputs; prereaction_status observes that request and evaluate_prereaction remeasures its registered poses. These are exploratory structure screens, not reactivity or activity validation. Shared MD handoff remains blocked until all gates and the delivery adapter are connected. workflow restores the current project's De novo study from durable evidence and plan receipts without submitting compute. plan_study compiles that study using registered assets, preserving target, pocket, warhead, slots and seed; missing evidence/assets reject without fallback. PLAN_READY is not approval or execution. Generate only from a frozen scientific study plan. create_study can request native remote_compute approval for this session's bound frozen study, preserving hypothesis evidence, slots and seed. A system notification is never approval; execution requires the exact native approval receipt. Other studies require the explicit user execution command. Legacy create returns NEEDS_SCIENTIFIC_DECISION without creating a task. status reads historical or current tasks in this session. Fixed EGFR WT C797 acrylamide profile; max 96 slots, one H100, 15 minutes, no refills.",
  parameters: z.discriminatedUnion("action", [
    z.object({ action: z.literal("registered_profile") }).strict()
      .describe("Read the supported PocketXMol target, site, receptor and exact registered pocket ID before creating a first-release study. Do not invent a descriptive pocket string."),
    z.object({ action: z.literal("warheads") }).strict(),
    z.object({ action: z.literal("branches"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional() }).strict(),
    z.object({ action: z.literal("branch_study"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional(),
      branch_id: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/) }).strict(),z.object({ action: z.literal("create") }).strict(),
    z.object({ action: z.literal("workflow"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional()
      .describe("Omit to restore the study already bound to this session.") }).strict(),
    z.object({ action: z.literal("plan_study"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional()
      .describe("Omit to compile the study already bound to this session.") }).strict(),
    z.object({ action: z.literal("prepare_evaluation"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional(),
      candidate_id: z.string().regex(/^denovo-[a-f0-9]{24}:[0-9]+:(raw|relaxed)$/) }).strict(),
    z.object({ action: z.literal("evaluation"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional() }).strict(),
    z.object({ action: z.enum(["register_evaluation", "plan_evaluation", "replan", "evaluate", "evaluation_status", "prepare_md_handoff",
      "plan_prereaction", "prereaction_status", "evaluate_prereaction"]),
      study_id: z.string().regex(/^study-[a-f0-9-]{36}$/).optional() }).strict(),
    z.object({ action: z.literal("create_study"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/) }).strict(),
    z.object({ action: z.literal("retry_study"), study_id: z.string().regex(/^study-[a-f0-9-]{36}$/) }).strict(),
    z.object({ action: z.enum(["select_batch", "prepare_batch", "register_batch", "plan_batch_vs", "batch_vs_status", "read_batch_vs_products", "prepare_batch_lo", "retry_batch_lo", "plan_batch_lo", "batch_lo_status", "evaluate_batch_lo", "prepare_batch_prereaction", "plan_batch_prereaction", "batch_prereaction_status", "evaluate_batch_prereaction", "report_batch"]), task_id: z.string().regex(/^denovo-[a-f0-9]{24}$/) })
      .describe("select_batch validates raw chemical identities and selects up to 100 diverse parents; prepare_batch freezes independent mapped inputs; register_batch registers those inputs remotely. These actions do not submit docking.").strict(),
    z.object({ action: z.literal("results"), task_id: z.string().regex(/^denovo-[a-f0-9]{24}$/) }).strict(),
    z.object({ action: z.literal("status"), task_id: z.string().regex(/^denovo-[a-f0-9]{24}$/) }).strict()]),
  async execute(args, ctx): Promise<{ title: string; metadata: {
    state: string; task_id?: string; compute_submitted?: boolean; truncated?: boolean
  }; output: string }> {
    if (args.action === "registered_profile") {
      const profile = await readStudyGenerationProfile((await registeredDeNovoSource()).package)
      return { title: "De novo · 已登记生成方案", metadata: { state: "REGISTERED_PROFILE", compute_submitted: false },
        output: JSON.stringify({ profile: profile.id, generator: "PocketXMol", warhead: profile.warhead,
          study_binding: profile.study_binding, remote_runtime_verified: false, compute_submitted: false,
          note: "Use the exact study_binding.pocket identifier only when the requested target/site/structure match. This is installed capability, not scientific validation or execution approval." }, null, 2) }
    }
    if (args.action === "warheads") return { title: "De novo · 战头研究目录",
      metadata: { state: "RESEARCH_CATALOG", compute_submitted: false }, output: JSON.stringify(warheadRegistry, null, 2) }
    if (args.action === "branches" || args.action === "branch_study") {
      const id = await sessionStudy({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID }, args.study_id)
      const result = args.action === "branches"
        ? await deNovoBranches(Instance.directory, Instance.project.id, id)
        : await materializeDeNovoBranch(Instance.directory, Instance.project.id, id, args.branch_id)
      return { title: "De novo · 多战头分支", metadata: { state: "BRANCH_REVIEW", compute_submitted: false },
        output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "create") return {
      title: "需要位点—弹头科学假说",
      metadata: { state: "NEEDS_SCIENTIFIC_DECISION", compute_submitted: false, truncated: true },
      output: "尚未提交计算。请先建立研究、完成有证据支持的位点—弹头假说并编译生成计划，再使用“执行 de-novo 研究 study-<id>”。直接指定 EGFR/C797/丙烯酰胺不能替代科学假说。",
    }
    if (args.action === "prepare_evaluation" || args.action === "evaluation") {
      const id = await sessionStudy({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID }, args.study_id)
      const study = await readStudy(Instance.directory, Instance.project.id, id)
      const result = args.action === "prepare_evaluation"
        ? await prepareDeNovoIndependentInput(Instance.directory, study, args.candidate_id)
        : await readDeNovoIndependentInput(Instance.directory, study)
      return { title: "De novo · 独立评价输入", metadata: { state: result?.state ?? "NOT_PERFORMED", compute_submitted: false },
        output: JSON.stringify(result ?? { state: "NOT_PERFORMED", compute_submitted: false,
          next: "Select one source-verified candidate and call prepare_evaluation; generation geometry is not independent evidence." }, null, 2) }
    }
    if (args.action === "register_evaluation" || args.action === "plan_evaluation" || args.action === "replan" || args.action === "evaluate"
      || args.action === "evaluation_status" || args.action === "prepare_md_handoff"
      || args.action === "plan_prereaction" || args.action === "prereaction_status" || args.action === "evaluate_prereaction") {
      const id = await sessionStudy({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID }, args.study_id)
      const study = await readStudy(Instance.directory, Instance.project.id, id)
      const result = args.action === "register_evaluation"
        ? await registerDeNovoIndependentInput(Instance.directory, study)
        : await syncDeNovoIndependentReview(Instance.directory, study,
            args.action === "plan_evaluation" ? "plan" : args.action === "evaluation_status" ? "status" : args.action)
      return { title: "De novo · 独立评价", metadata: { state: "INDEPENDENT_REVIEW", compute_submitted: false },
        output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "workflow" || args.action === "plan_study") {
      const scope = { directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID }
      const id = await sessionStudy(scope, args.study_id)
      if (args.action === "plan_study") {
        const study = await readStudy(Instance.directory, Instance.project.id, id)
        if (study.denovoBranches) throw Error("Select a child branch study; a parent campaign cannot compile one shared plan")
        const saved = await readDeNovoPlan(Instance.directory, study)
        if (!saved) {
          const capability = await deNovoCapability(study)
          if (capability.state !== "REGISTERED_PROFILE_MATCH") return { title: "De novo · 等待能力接入",
            metadata: { state: "WAITING_CAPABILITY", compute_submitted: false }, output: JSON.stringify(capability, null, 2) }
        }
        await compileDeNovoPlan(Instance.directory, study)
      }
      const current = await readStudy(Instance.directory, Instance.project.id, id)
      if (current.denovoBranches) return { title: "De novo · 多战头分支",
        metadata: { state: "BRANCH_REVIEW", compute_submitted: false },
        output: JSON.stringify(await deNovoBranches(Instance.directory, Instance.project.id, id), null, 2) }
      const workflow = await deNovoWorkflow(Instance.directory, Instance.project.id, id)
      await bindStudySession(scope, id)
      return { title: `De novo · ${workflow.stage}`,
        metadata: { state: workflow.stage, compute_submitted: false },
        output: JSON.stringify({ ...workflow, new_plan_capability: await deNovoCapability(current),
          recovery_policy: "Existing frozen plans keep their recorded source and native approval scope" }, null, 2) }
    }
    if (args.action === "report_batch") {
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID, "report_batch")
      return { title: "De novo · 候选排序报告", metadata: { state: "BATCH_REPORT_READY",
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "plan_batch_prereaction" || args.action === "batch_prereaction_status" || args.action === "evaluate_batch_prereaction") {
      const action = args.action === "plan_batch_prereaction" ? "plan_prereaction_batch"
        : args.action === "batch_prereaction_status" ? "status_prereaction_batch" : "evaluate_prereaction_batch"
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID, action)
      return { title: args.action === "plan_batch_prereaction" ? "De novo · Glide 计划" : "De novo · 反应前状态与评价",
        metadata: { state: args.action === "plan_batch_prereaction" ? "BATCH_GLIDE_PLAN_READY" : "BATCH_GLIDE_OBSERVED",
          task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "prepare_batch_prereaction") {
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID, "prepare_prereaction_batch")
      return { title: "De novo · 独立反应前输入", metadata: { state: "BATCH_PREREACTION_INPUT_FROZEN",
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "retry_batch_lo") {
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID, "retry_lo")
      return { title: "De novo · LO 修复后输入", metadata: { state: "BATCH_LO_RETRY_INPUT_FROZEN",
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "batch_lo_status" || args.action === "evaluate_batch_lo") {
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID,
        args.action === "evaluate_batch_lo" ? "evaluate_lo" : "status_lo")
      return { title: args.action === "evaluate_batch_lo" ? "De novo · LO 结构评价" : "De novo · LO 状态",
        metadata: { state: "BATCH_LO_OBSERVED", task_id: args.task_id, compute_submitted: false },
        output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "batch_vs_status" || args.action === "read_batch_vs_products" || args.action === "prepare_batch_lo" || args.action === "plan_batch_lo") {
      const action = args.action === "plan_batch_lo" ? "plan_lo" : args.action === "prepare_batch_lo" ? "prepare_lo" : args.action === "read_batch_vs_products" ? "read_vs_products" : "status_vs"
      const result = await statusDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID, action)
      return { title: args.action === "plan_batch_lo" ? "De novo · LO 计划" : args.action === "prepare_batch_lo" ? "De novo · LO 输入已冻结" : "De novo · VS 状态",
        metadata: { state: args.action === "plan_batch_lo" ? "BATCH_LO_PLAN_READY" : args.action === "prepare_batch_lo" ? "BATCH_LO_INPUT_FROZEN" : "BATCH_VS_OBSERVED",
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "plan_batch_vs") {
      const result = await planDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID)
      return { title: "De novo · VS 计划", metadata: { state: "BATCH_VS_PLAN_READY",
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "register_batch") {
      const result = await registerDeNovoBatch(args.task_id, Instance.directory, ctx.sessionID)
      return { title: "De novo · 批量输入登记", metadata: { state: result.state,
        task_id: args.task_id, compute_submitted: false }, output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "select_batch" || args.action === "prepare_batch") {
      const result = await (args.action === "prepare_batch" ? prepareDeNovoBatch : selectDeNovoBatch)(args.task_id, Instance.directory, ctx.sessionID)
      return { title: "De novo · 化学检查与多样性筛选",
        metadata: { state: args.action === "prepare_batch" ? "BATCH_INPUT_FROZEN" : "BATCH_SELECTED", task_id: args.task_id, compute_submitted: false },
        output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "results") {
      const result = await readDeNovoResults(tasks, args.task_id,
        { directory: Instance.directory, session: ctx.sessionID })
      return { title: `De novo · ${result.state}`,
        metadata: { state: result.state, task_id: args.task_id, compute_submitted: false },
        output: JSON.stringify(result, null, 2) }
    }
    if (args.action === "status") {
      const status = await readDeNovoTask(tasks, args.task_id,
        { directory: Instance.directory, session: ctx.sessionID })
      if (!status) throw Error("Native task does not exist")
      await acknowledgeDeNovoCompute({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID },
        "generation", args.task_id, args.task_id, status.state)
      return { title: `De novo · ${status.state}`,
        metadata: { task_id: args.task_id, state: status.state },
        output: JSON.stringify({ task: status, assessment: "not_evaluated_by_status",
          compute_submitted_by_this_call: false }, null, 2) }
    }
    if (args.action !== "create_study" && args.action !== "retry_study") throw Error("Unsupported native De novo action")
    const studyID = z.string().regex(/^study-[a-f0-9-]{36}$/).parse(args.study_id)
    const study = await readStudy(Instance.directory, Instance.project.id, studyID)
    const saved = await readDeNovoPlan(Instance.directory, study)
    if (!saved) throw Error("Compile and freeze this study's de-novo plan first")
    // A current-release pointer is for new plans. Recovery must use the release
    // recorded by this already frozen plan, including after a platform upgrade.
    const source = await registeredDeNovoSource(undefined, saved.plan.request.source_release)
    const profile = z.object({ local_python: z.string(), allowed_directories: z.array(z.string()), max_slots: z.number(),
      cli: z.string(), account: z.string(), workspace: z.string() }).passthrough().parse(await Bun.file(path.join(source.package, "profile.json")).json())
    if (!profile.allowed_directories.includes(Instance.directory)) throw new Error("This project has no registered de novo profile")
    if (args.action === "retry_study") {
      const id = z.string().regex(/^denovo-[a-f0-9]{24}$/).parse(saved.plan.request.id)
      const root = path.join(tasks, id)
      const current = await readDeNovoTask(tasks, id, { directory: Instance.directory, session: ctx.sessionID,
        study: studyID, request: saved.plan.request })
      if (!current || !current.approval_verified) throw Error("The original approved generation task is unavailable")
      if (current.state !== "NEEDS_REVIEW") return { title: `EGFR C797 · ${current.state}`,
        metadata: { task_id: id, state: current.state, compute_submitted: false }, output: report(current) }
      const receipt = await inspectGenerationRetry(root, id, async () => observeMissingInspireJob(profile, id))
      const retryPlan = JSON.stringify({ ...receipt, study_id: studyID, original_request_sha256:
        digest(await Bun.file(path.join(root, "request.json")).text()) })
      const fingerprint = digest(retryPlan)
      ctx.metadata({ title: "等待 de novo 配额恢复续提审批", metadata: { task_id: id, state: "AWAITING_RETRY_APPROVAL", digest: fingerprint } })
      await ctx.ask({ permission: "remote_compute", patterns: [fingerprint], always: [], metadata: {
        route: "denovo", task_id: id, retry_attempt: 2, retry_plan_sha256: fingerprint,
        reason: "首次提交被项目 GPU 配额明确拒绝，且同名远端任务已确认不存在",
        compute: { provider: "inspire", task_id: id, account: profile.account, workspace: profile.workspace,
          gpu: "1 × H100", max_minutes: 15, immutable_retry_plan: JSON.parse(retryPlan),
          warning: "仅允许一次续提；保留首次失败、原审批和冻结计划。任何远端任务存在或状态不确定都会拒绝续提。" },
      } })
      ctx.abort.throwIfAborted()
      await writeFile(path.join(root, "retry-authorization.json"), JSON.stringify({ request_sha256: fingerprint,
        session_id: ctx.sessionID, approved_at: Date.now(), mechanism: "native_remote_compute", retry_attempt: 2,
        receipt: JSON.parse(retryPlan) }, null, 2) + "\n", { flag: "wx", mode: 0o600 })
      await armGenerationRetry(root, JSON.parse(retryPlan))
      await registerDeNovoCompute({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID },
        studyID, "generation", id, id)
      launch(root, profile.local_python)
      return { title: "De novo · 配额恢复续提已启动", metadata: { task_id: id, state: "PREPARING", compute_submitted: true },
        output: JSON.stringify({ task_id: id, state: "PREPARING", retry_attempt: 2,
          previous_failure_preserved: true, terminal_notification_armed: true }, null, 2) }
    }
    const assistant = await MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.messageID })
    if (assistant.info.role !== "assistant") throw new Error("Missing original assistant/user binding")
    const user = await MessageV2.get({ sessionID: ctx.sessionID, messageID: assistant.info.parentID })
    if (user.info.role !== "user") throw new Error("Missing original user request")
    const binding = await boundSessionStudy({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID })
    const requestApproval = binding?.id === args.study_id && binding.routes["de-novo"].selected
      && binding.routes["de-novo"].mode === "new" && !binding.routes["library-first"].selected && !binding.routes["ligand-first"].selected
    // Notifications can request the exact native approval, never supply user authorization.
    const original = user.parts.some(p => p.type === "text" && !p.synthetic && !p.ignored && p.text.trim()) ? user
      : requestApproval ? (await Session.messages({ sessionID: ctx.sessionID })).findLast(item => item.info.role === "user"
        && item.info.id < user.info.id && item.parts.some(p => p.type === "text" && !p.synthetic && !p.ignored && p.text.trim())) : undefined
    if (!original || original.info.role !== "user") throw Error("Original user request unavailable; cannot request approval")
    const text = original.parts.filter((p) => p.type === "text" && !p.synthetic && !p.ignored).map((p) => p.type === "text" ? p.text : "").join("\n")
    const bound = await (async () => {
      return bindStudyDeNovoRequest(saved.plan, args.study_id, { directory: Instance.directory,
        session: ctx.sessionID, message: original.info.id, text, package: source.package, worker: source.worker }, requestApproval)
    })()
    const id = bound.id
    const root = path.join(tasks, id)
    {
      if (!bound) throw Error("A validated scientific study plan is required")
      await ctx.ask({ permission: "denovo_generate", patterns: [bound.profile], always: [], metadata: { profile: bound.profile, slots: bound.slots } })
      await mkdir(tasks, { recursive: true, mode: 0o700 })
      // mkdir is the atomic per-message reservation. Existing requests are never replaced.
      const created = await mkdir(root, { mode: 0o700 }).then(() => true, (error: NodeJS.ErrnoException) => { if (error.code === "EEXIST") return false; throw error })
      if (created) {
        await cp(source.package, path.join(root, "package"), { recursive: true, filter: (name) => !name.includes("__pycache__") })
        await cp(source.worker, path.join(root, "worker.py"))
        const request = bound
        await writeFile(path.join(root, "request.json"), JSON.stringify(request, null, 2) + "\n", { flag: "wx", mode: 0o600 })
        await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "AWAITING_APPROVAL" }))
      }
    }
    const request = z.object({ session_id: z.string(), directory: z.string() }).passthrough().parse(await Bun.file(path.join(root, "request.json")).json())
    if (request.session_id !== ctx.sessionID || request.directory !== Instance.directory) throw new Error("Task belongs to another session/project")
    const recovered = await readDeNovoTask(tasks, id, {
      directory: Instance.directory, session: ctx.sessionID, study: args.study_id,
      request: { ...bound, session_id: bound.planning_session_id },
    })
    if (!recovered || recovered.state === "OUTCOME_UNKNOWN")
      throw Error("Native task reservation is incomplete; inspect without resubmission")
    {
      const plan = await Bun.file(path.join(root, "request.json")).text()
      const fingerprint = digest(plan)
      const current = statusSchema.parse(await Bun.file(path.join(root, "status.json")).json())
      if (["COMPLETED", "CANCELLED", "FAILED", "NEEDS_REVIEW"].includes(current.state)) {
        await acknowledgeDeNovoCompute({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID },
          "generation", id, id, current.state)
        return { title: `EGFR C797 · ${current.state}`, metadata: { task_id: id, state: current.state }, output: report(current) }
      }
      if (!await Bun.file(path.join(root, "authorization.json")).exists()) {
        ctx.metadata({ title: "等待 de novo 算力审批", metadata: { task_id: id, state: "AWAITING_APPROVAL", digest: fingerprint } })
        await ctx.ask({ permission: "remote_compute", patterns: [fingerprint], always: [], metadata: {
          route: "denovo", task_id: id, execution_plan_sha256: fingerprint, profile: request.profile,
          target: "EGFR WT · 4G5J A:C797", warhead: "固定丙烯酰胺，N-only 生长", slots: request.slots,
          engine: "PocketXMol", gpu: "1 × H100", max_minutes: 15, max_points: 0.25, no_refills: true,
          output: "新生成 SDF、逐分子验收 CSV、结构图；自动保存 Results", immutable_plan: request,
          compute: {
            provider: "inspire", task_id: id, digest: fingerprint,
            purpose: `EGFR C797 固定丙烯酰胺：PocketXMol 从头生成 ${request.slots} 个槽位 → 结构验收 → 自动保存 Results`,
            candidates: request.slots, minutes: 15, maximum_compute_points: 0.25,
            account: profile.account, notebook: profile.notebook, workspace: profile.workspace,
            project: profile.project, group: profile.group, quota: profile.quota, image: profile.image,
            source_manifest_digest: digest(JSON.stringify(request.package_hashes)),
            warning: "单次 1 张 H100，最多 15 分钟，当前单价上限 1 点券/小时；不自动补样或重跑。蛋白为 EGFR WT 4G5J A:C797；固定丙烯酰胺五个重原子，只从 N 生长。批准后自动完成生成、原始及优化后结构验收、SDF/CSV/结构图交付。结构通过不代表活性或共价键得到验证。",
          },
        } }).catch(async (error: unknown) => {
          await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "CANCELLED", error: "未获得本次精确算力计划的原生批准，未提交计算。" }))
          throw error
        })
        ctx.abort.throwIfAborted()
        // Only the native permission resolver can reach this receipt. Models
        // cannot supply an approval flag or alter this immutable request.
        await writeFile(path.join(root, "authorization.json"), JSON.stringify({ request_sha256: fingerprint, session_id: ctx.sessionID, user_message_id: request.user_message_id, approved_at: Date.now(), mechanism: "native_remote_compute" }), { flag: "wx", mode: 0o600 })
      }
      await registerDeNovoCompute({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID },
        args.study_id, "generation", id, id)
      launch(root, profile.local_python)
    }
    const deadline = Date.now() + 45 * 60 * 1000
    while (true) {
      const status = statusSchema.parse(await Bun.file(path.join(root, "status.json")).json())
      ctx.metadata({ title: `EGFR C797 · ${status.state}`, metadata: { task_id: id, state: status.state, requested: request } })
      if (["COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW"].includes(status.state) || Date.now() > deadline) {
        await acknowledgeDeNovoCompute({ directory: Instance.directory, project: Instance.project.id, session: ctx.sessionID },
          "generation", id, id, status.state)
        return { title: `EGFR C797 · ${status.state}`, metadata: { task_id: id, state: status.state }, output: report(status) }
      }
      if (ctx.abort.aborted) {
        await Bun.write(path.join(root, "cancel"), "User aborted active generation\n")
        throw new Error("Cancellation requested; the worker will stop the assigned job")
      }
      await Bun.sleep(2000)
    }
  },
})
