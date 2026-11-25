import z from "zod"
import { randomBytes } from "node:crypto"
import path from "node:path"
import type { ToolContext } from "@synsci/plugin/tool"
import { digest } from "./model"
import { requestHash, type BoundRequest } from "./request"
import type { Store } from "./store"
import { label, screeningMode, type Choice } from "./resource-planner"
import { parseTargetParameters, targetParameters, targetSite } from "@synsci/util/screening-target"

type QuestionInfo = { header: string; question: string; options: { label: string; description: string }[]; custom: boolean }
type NativeQuestions = {
  ask(input: { sessionID: string; questions: QuestionInfo[]; tool: { messageID: string; callID: string } }): Promise<string[][]>
  list(): Promise<{ id: string; sessionID: string; tool?: { messageID: string; callID: string } }[]>
  reject(id: string): Promise<void>
  RejectedError: new () => Error
}
class IntakeDismissed extends Error {}

export const conditions = z.object({ target: targetParameters.shape.target, site: z.string().regex(/^[A-Z0-9]+-?\d+[A-Za-z0-9]?$/),
  target_parameters: targetParameters.optional(),
  library: z.literal("CovInDB/Virtual_Screening_Library.csv"), screening_mode: screeningMode,
  requested_mode: screeningMode.optional(), max_points: z.number().finite().nonnegative().nullable(),
}).strict()
type Conditions = z.infer<typeof conditions>
const groups = [
  { id: "science", header: "靶点与候选库", fields: ["target", "site", "library"] as const,
    example: "EGFR C797；CovInDB 虚拟筛选库" },
  { id: "automation", header: "筛选模式与预算", fields: ["screening_mode", "max_points"] as const,
    example: "标准筛选；预算不超过0.5点券" },
]

// Conservative recognition, not a general NLP parser. Ambiguous/negated/history
// text is clarified by the user. Model arguments are suggestions, never evidence.
export function explicitConditions(text: string): Partial<Conditions> {
  const parameters = parseTargetParameters(text)
  const prose = text.split("\n").filter((line) => !line.startsWith("target_parameters=")).join("\n")
  const unsafe = /不要|不是|并非|不再|无需|不用|不能|不做|禁止|排除|未确认|待定|之前|历史|旧的|旧任务|旧候选|例如|示例|举例|改成|或者|二选一|比较|对比|已知抑制剂|CovInDB_All|Natural_Compounds|天然产物|\b(?:not|previous|example|instead|or|vs|pilot)\b/i
  // A safety clause such as "不要使用旧候选" must not erase an earlier,
  // explicit target or budget. Ignore only the ambiguous/negated clause.
  const safe = prose.split(/[\n。；;，,]/).map((part) => part.trim()).filter((part) => part && !unsafe.test(part)).join("；")
  const result: Partial<Conditions> = {}
  if (/\bEGFR\b/i.test(safe)) result.target = "EGFR"
  if (/\bC797\b/i.test(safe)) result.site = "C797"
  if (parameters) {
    if ((result.target && result.target !== parameters.target) || (result.site && result.site !== targetSite(parameters)))
      throw new Error("文字靶点与 target_parameters 冲突，请只提供本次任务条件。")
    Object.assign(result, { target: parameters.target, site: targetSite(parameters), target_parameters: parameters })
  }
  if (/(?:CovInDB|CovalentInDB)[\s/·：:]*.*(?:虚拟筛选库|Virtual_Screening_Library\.csv)/i.test(safe)) {
    result.library = "CovInDB/Virtual_Screening_Library.csv"
  }
  const modes = [...new Set([
    ...(/screening_mode\s*[:=：]\s*research_pilot_v1\b/i.test(safe) ? ["research_pilot_v1" as const] : []),
    ...(/(?:100\s*(?:个)?\s*(?:候选)?\s*测试(?:档|模式)?|测试\s*100\s*(?:个)?|screening_mode\s*[:=：]\s*test100\b)/i.test(safe) ? ["test100" as const] : []),
    ...(/(?:经济(?:模式|筛选)|screening_mode\s*[:=：]\s*economy\b)/i.test(safe) ? ["economy" as const] : []),
    ...(/(?:标准(?:模式|筛选)|screening_mode\s*[:=：]\s*standard\b)/i.test(safe) ? ["standard" as const] : []),
    ...(/(?:深度(?:模式|筛选)|screening_mode\s*[:=：]\s*deep\b)/i.test(safe) ? ["deep" as const] : []),
  ])]
  if (modes.length === 1) result.screening_mode = modes[0]
  const budgets = [...new Set([...safe.matchAll(/(?:max_points\s*[:=：]\s*|预算(?:上限)?\s*[:=：]?\s*|预算(?:不超过|不高于|最多)\s*)(\d+(?:\.\d+)?)\s*(?:点券)?(?=$|[\s;；,，。])/gi)].map((match) => Number(match[1])))]
  const uncapped = safe.split("；").some((part) => /^(?:(?:本次)?测试\s*)?(?:(?:点[券劵]|预算)(?:金额)?\s*(?:不设|不限制|取消)\s*(?:金额)?上限|(?:点[券劵]|预算)\s*不限|(?:不设|不限制|取消)\s*(?:点[券劵]|预算)(?:金额)?(?:的)?上限)[。.]?$/.test(part))
  if (budgets.length === 1 && !uncapped) result.max_points = budgets[0]
  if (uncapped && !budgets.length && result.screening_mode === "test100") result.max_points = null
  return result
}

function questions(source: BoundRequest, proposed: Partial<Conditions>, requireTarget: boolean) {
  const explicit = explicitConditions(source.text)
  const scientific = { id: "target_parameters", header: "受体与位点参数", fields: ["target_parameters"] as const,
    example: '单独一行 target_parameters={"target":"靶点名称","receptor":"受体标识或路径","chain":"A","residue":"CYS","residue_number":位点编号,"insertion_code":"","numbering":"canonical","reaction_type":"Michael Addition"}' }
  const entries = [...groups, ...(requireTarget ? [scientific] : [])].map((group) => {
    const validGroup = (value: Record<string, unknown>) => group.fields.every((key) => value[key] !== undefined && conditions.shape[key].safeParse(value[key]).success)
    const proven = Object.fromEntries(group.fields.map((key) => [key, explicit[key]]))
    const complete = validGroup(proven)
    if (complete) return { group, proven, question: null, suggested: null }
    const suggested = Object.fromEntries(group.fields.map((key) => [key, Object.hasOwn(explicit, key) ? explicit[key] : proposed[key]]))
    const valid = validGroup(suggested)
    return { group, proven, suggested: valid ? suggested : null, question: {
      header: group.header,
      question: `请明确${group.header}。${valid ? `待你确认的建议：${JSON.stringify(suggested)}。` : ""}可自行输入，例如：${group.example}。例子不是默认值；候选从源库重新准备，不沿用旧候选。这里只确认条件，不批准计算。`,
      options: valid ? [{ label: "采用上述建议", description: "明确采用问题中列出的条件，稍后仍需资源审批。" },
        { label: "暂不创建任务", description: "停止本次准备，不创建筛选任务或提交计算。" }] : [],
      custom: true,
    } satisfies QuestionInfo }
  })
  return { explicit, entries }
}

export async function confirmPlanning(store: Store, id: string, choice: Choice, context: Context) {
  if (!choice.requires_confirmation) return conditions.parse(JSON.parse((store.db.query("SELECT resolved FROM intakes WHERE id=?").get(id) as { resolved: string }).resolved))
  const row = store.db.query("SELECT * FROM intakes WHERE id=?").get(id) as {
    state: string; session_id: string; directory: string; resolved: string; resolved_digest: string;
    answers: string | null; questions: string } | null
  if (!row || row.state !== "CONFIRMED" || row.session_id !== context.sessionID || row.directory !== context.directory) {
    throw new Error("Resource adjustment belongs to another confirmed intake")
  }
  const current = conditions.parse(JSON.parse(row.resolved))
  if (digest(current) !== row.resolved_digest || current.screening_mode !== choice.requested || current.max_points !== choice.max_points) {
    throw new Error("Resource adjustment differs from the confirmed request")
  }
  const budget = Math.ceil(choice.estimated_points * 1_000_000) / 1_000_000
  const raise = choice.reason === "minimum_budget_required"
  const action = raise ? `将预算提高到 ${budget} 点券` : `采用${label(choice.selected)}方案`
  const question: QuestionInfo = { header: "预算适配", custom: false,
    question: raise
      ? `按当前 Inspire 实时单价，${label(choice.selected)}模式预计需要 ${budget} 点券，高于你给出的 ${current.max_points} 点券。是否提高本任务预算上限？这里只调整待审批方案，不批准或启动计算。`
      : `按当前 Inspire 实时单价，${label(choice.requested)}模式预计需要 ${choice.estimated_points.toFixed(6)} 点券以上，超过 ${current.max_points} 点券上限。可改为预算内的${label(choice.selected)}模式；不会静默缩小范围。`,
    options: [{ label: action, description: raise ? "提高本任务上限，随后仍需查看并批准完整资源卡。" : "明确接受较小科学范围，随后仍需资源审批。" },
      { label: "暂不创建任务", description: "保留预算边界，不创建筛选任务或提交计算。" }] }
  store.db.transaction(() => {
    const changed = store.db.query("UPDATE intakes SET state='PLANNING',questions=? WHERE id=? AND state='CONFIRMED'")
      .run(JSON.stringify([...JSON.parse(row.questions), { group: { id: "budget_adjustment" }, question }]), id).changes
    if (changed !== 1) throw new Error("Resource adjustment state changed")
  })()
  try {
    context.metadata({ title: "预算不足，等待确认调整（尚未创建任务）", metadata: { intake_id: id, compute_approved: false } })
    const answers = await ask([question], context)
    if (answers.length !== 1 || answers[0].length !== 1) throw new Error("Budget adjustment needs exactly one answer")
    if (answers[0][0] === "暂不创建任务") throw new IntakeDismissed("User deferred screening creation")
    if (answers[0][0] !== action) throw new Error("Budget adjustment answer differs from the offered plan")
    const resolved = conditions.parse({ ...current, requested_mode: current.screening_mode,
      screening_mode: choice.selected, ...(raise ? { max_points: budget } : {}) })
    store.db.query("UPDATE intakes SET state='CONFIRMED',answers=?,resolved=?,resolved_digest=? WHERE id=? AND state='PLANNING'")
      .run(JSON.stringify([...(row.answers ? JSON.parse(row.answers) : []), answers[0]]), JSON.stringify(resolved), digest(resolved), id)
    return resolved
  } catch (error) {
    store.db.query("UPDATE intakes SET state=? WHERE id=? AND state='PLANNING'")
      .run(context.abort.aborted ? "ABORTED" : error instanceof IntakeDismissed ? "REJECTED" : "INVALID", id)
    throw error
  }
}

type Context = Pick<ToolContext, "sessionID" | "messageID" | "directory" | "abort" | "metadata"> & { callID?: string }

async function ask(questions: QuestionInfo[], context: Context) {
  // Source-host bridge: these variable imports remain external to the plugin
  // bundle, retaining the running app's actual Question/Instance singletons.
  // The registered bundle lives beside this source, as host.ts also requires.
  const question = path.resolve(import.meta.dir, "../../../src/question/index.ts")
  const instance = path.resolve(import.meta.dir, "../../../src/project/instance.ts")
  const Question = (await import(question)).Question as NativeQuestions
  const Instance = (await import(instance)).Instance as { directory: string;
    provide<T>(input: { directory: string; fn: () => Promise<T> }): Promise<T> }
  if (Instance.directory !== context.directory || !context.callID) throw new Error("Native clarification is not bound to the active platform tool")
  context.abort.throwIfAborted()
  const before = new Set((await Question.list()).map((item) => item.id))
  const pending = Question.ask({ sessionID: context.sessionID, questions,
    tool: { messageID: context.messageID, callID: context.callID } }).then(
    (answers) => ({ answers }), (error: unknown) => ({ error }))
  const matches = (await Question.list()).filter((item) => !before.has(item.id) && item.sessionID === context.sessionID &&
    item.tool?.messageID === context.messageID && item.tool?.callID === context.callID)
  if (matches.length > 1) throw new Error("Cannot bind exactly one native clarification card")
  // A reply may already have removed this request before the list read. Its
  // own promise still carries the answer; do not mistake a fast reply for loss.
  const stopped = Promise.withResolvers<void>()
  const cleanup = { promise: Promise.resolve() }
  const abort = () => {
    if (matches[0]) cleanup.promise = Instance.provide({ directory: context.directory,
      fn: () => Question.reject(matches[0].id) }).catch(() => undefined)
    stopped.resolve()
  }
  context.abort.addEventListener("abort", abort, { once: true })
  if (context.abort.aborted) abort()
  try {
    const result = await Promise.race([pending, stopped.promise.then(() => {
      context.abort.throwIfAborted()
      throw new Error("Screening clarification interrupted")
    })])
    context.abort.throwIfAborted()
    if ("error" in result) {
      if (result.error instanceof Question.RejectedError) throw new IntakeDismissed("User dismissed screening clarification")
      throw result.error
    }
    return result.answers
  } finally { context.abort.removeEventListener("abort", abort); await cleanup.promise }
}

export async function clarify(store: Store, source: BoundRequest, proposed: Partial<Conditions>, context: Context, requireTarget = false) {
  context.abort.throwIfAborted()
  if (source.origin.assistant_message_id !== context.messageID || requestHash(source.text) !== source.origin.request_sha256) {
    throw new Error("Clarification source differs from the bound user request")
  }
  const prior = store.db.query("SELECT * FROM intakes WHERE session_id=? AND user_message_id=? AND request_sha256=?")
    .get(context.sessionID, source.origin.user_message_id, source.origin.request_sha256) as
      { id: string; directory: string; state: string; resolved: string | null; resolved_digest: string | null; task_id: string | null } | null
  if (prior) {
    if (prior.directory !== context.directory) throw new Error("Intake belongs to another project")
    if (prior.task_id) throw new Error(`Original request already created ${prior.task_id}; recover that task, never create a duplicate`)
    if (prior.state !== "CONFIRMED" || !prior.resolved) throw new Error("Prior clarification is pending, dismissed or interrupted; wait for it or provide a new user request")
    const values = conditions.parse(JSON.parse(prior.resolved))
    if (requireTarget && !values.target_parameters) throw new Error("旧请求缺少受体与位点参数，请发送包含完整参数的新请求；不会补写旧批准。")
    if (digest(values) !== prior.resolved_digest) throw new Error("Confirmed intake changed")
    return { id: prior.id, values }
  }
  const prepared = questions(source, proposed, requireTarget)
  const pending = prepared.entries.filter((entry) => entry.question !== null)
  const id = "intake-" + randomBytes(12).toString("hex")
  store.db.query(`INSERT INTO intakes(id,session_id,directory,user_message_id,request_sha256,origin,questions,state,created)
    VALUES(?,?,?,?,?,?,?,'WAITING',?)`).run(id, context.sessionID, context.directory, source.origin.user_message_id,
    source.origin.request_sha256, JSON.stringify(source.origin), JSON.stringify(pending), Date.now())
  try {
    context.metadata({ title: pending.length ? "先确认筛选条件（尚未创建任务）" : "核对原始筛选条件", metadata: { intake_id: id, compute_approved: false } })
    const answers = pending.length ? await ask(pending.map((entry) => entry.question!), context) : []
    store.db.query("UPDATE intakes SET answers=? WHERE id=? AND state='WAITING'").run(JSON.stringify(answers), id)
    context.abort.throwIfAborted()
    if (answers.length !== pending.length || answers.some((answer) => answer.length !== 1)) throw new Error("Every clarification needs exactly one explicit answer")
    const values = { ...prepared.explicit }
    for (const [index, entry] of pending.entries()) {
      const answer = answers[index][0]
      if (answer === "暂不创建任务") throw new IntakeDismissed("User deferred screening creation")
      const chosen = answer === "采用上述建议" && entry.suggested ? entry.suggested : explicitConditions(answer)
      for (const key of entry.group.fields) Object.assign(values, { [key]: chosen[key] })
    }
    const resolved = conditions.parse(values)
    if (resolved.target_parameters && (resolved.target !== resolved.target_parameters.target || resolved.site !== targetSite(resolved.target_parameters)))
      throw new Error("靶点/位点与任务参数冲突，请重新明确本次条件。")
    store.db.query("UPDATE intakes SET state='CONFIRMED',answers=?,resolved=?,resolved_digest=? WHERE id=? AND state='WAITING'")
      .run(JSON.stringify(answers), JSON.stringify(resolved), digest(resolved), id)
    return { id, values: resolved }
  } catch (error) {
    store.db.query("UPDATE intakes SET state=? WHERE id=? AND state='WAITING'")
      .run(context.abort.aborted ? "ABORTED" : error instanceof IntakeDismissed ? "REJECTED" : "INVALID", id)
    throw error
  }
}

export function bindIntake(store: Store, id: string, plan: import("./model").Plan) {
  const row = store.db.query("SELECT * FROM intakes WHERE id=?").get(id) as {
    state: string; session_id: string; directory: string; request_sha256: string; resolved: string; resolved_digest: string; task_id: string | null } | null
  if (!row || row.state !== "CONFIRMED" || row.session_id !== plan.session || row.directory !== plan.directory ||
    row.request_sha256 !== plan.origin?.request_sha256 || row.task_id) throw new Error("Task requires its own unconsumed confirmed intake")
  const saved = conditions.parse(JSON.parse(row.resolved))
  if (digest(saved) !== row.resolved_digest || !plan.automation || plan.automation.intent_digest !== row.resolved_digest ||
    plan.target !== saved.target || plan.site !== saved.site || plan.library !== saved.library ||
    JSON.stringify(plan.target_parameters) !== JSON.stringify(saved.target_parameters) ||
    plan.max_points !== saved.max_points || plan.automation.selected_mode !== saved.screening_mode ||
    plan.automation.requested_mode !== (saved.requested_mode ?? saved.screening_mode)) {
    throw new Error("Task conditions differ from the user's confirmed intake")
  }
  return () => {
    const changed = store.db.query("UPDATE intakes SET task_id=? WHERE id=? AND task_id IS NULL").run(plan.id, id).changes
    if (changed !== 1) throw new Error("Intake already consumed")
    store.event(plan.id, "intake_confirmed", { intake_id: id, request_sha256: row.request_sha256, conditions_digest: row.resolved_digest })
  }
}
