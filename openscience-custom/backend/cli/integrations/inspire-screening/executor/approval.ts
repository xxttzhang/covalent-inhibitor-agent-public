import type { ToolContext } from "@synsci/plugin/tool"
import { digest, type Plan } from "./model"
import type { Store } from "./store"
import { bindIntake } from "./intake"
import { progress } from "./progress"

type Context = Pick<ToolContext, "sessionID" | "directory" | "abort" | "metadata" | "ask">

// Persist BEFORE asking. The caller cannot return a draft to the model and
// accidentally leave the next step to prose or another model tool choice.
export async function propose(store: Store, plan: Plan, context: Context, intake?: string) {
  if (plan.session !== context.sessionID || plan.directory !== context.directory)
    throw new Error("Proposal belongs to another conversation/project")
  context.abort.throwIfAborted()
  const task = store.db.transaction(() => {
    const bind = intake ? bindIntake(store, intake, plan) : undefined
    const task = store.create(plan)
    bind?.()
    return task
  })()
  return approve(store, task.id, context)
}

export async function approve(store: Store, id: string, context: Context) {
  const task = store.get(id)
  if (task.state !== "DRAFT" || task.plan.session !== context.sessionID || task.plan.directory !== context.directory) {
    throw new Error("Only this conversation's unchanged draft can be approved")
  }
  try {
    if (Date.now() > task.plan.approve_before) throw new Error("Resource plan expired before approval")
    context.abort.throwIfAborted()
    context.metadata({
      title: "等待 Inspire 资源审批",
      metadata: { task_id: task.id, digest: task.digest, state: "DRAFT" },
    })
    store.event(task.id, "approval_requested", { digest: task.digest, session: context.sessionID })
    await context.ask({
      permission: "remote_compute",
      patterns: [task.digest],
      always: [],
      metadata: {
        compute: {
          provider: "inspire",
          task_id: task.id,
          digest: task.digest,
          planner: task.plan.automation ?? "legacy-manual-plan",
          purpose: "从虚拟筛选库重新准备 → CarsiDock → RTMScore → 验收报告",
          account: task.plan.connection.account,
          notebook: task.plan.connection.notebook,
          candidates: task.plan.shortlist,
          minutes: task.plan.steps.reduce((sum, step) => sum + step.minutes, 0),
          project: task.plan.steps[0].resource.project,
          group: task.plan.steps.map((step) => `${step.phase}: ${step.resource.group}`).join("; "),
          quota: task.plan.steps.map((step) => `${step.phase}: ${step.resource.quota}, ${step.minutes} min`).join("; "),
          estimated_compute_points: task.plan.automation?.estimated_points,
          maximum_compute_points: task.plan.max_points,
          budget_policy: task.plan.max_points === null ? "uncapped-test100-v1" : "capped",
          points_headroom: task.plan.automation?.budget_headroom,
          target: `${task.plan.target} ${task.plan.site}`,
          target_parameters: task.plan.target_parameters,
          library: task.plan.library,
          source_scope: task.plan.scope,
          max_records: task.plan.max_records,
          seed: task.plan.seed,
          quality_gates: task.plan.gates,
          preparation_cohort: task.plan.automation?.selected_mode === "test100"
            ? "必须有100个质检合格候选；不足时停止，不自动减少数量或放宽筛选条件" : undefined,
          delivery_policy: task.plan.delivery,
          image: task.plan.steps.map((step) => `${step.phase}: ${step.resource.image}`).join("; "),
          structure_site: "1M17 A:CYS:773 → EGFR C797 (+24)",
          warning: `平台已按 Inspire 实时配额单价将${task.plan.automation ? `${task.plan.automation.selected_mode}模式` : "人工方案"}冻结为：源库范围 ${task.plan.scope}，最多读取 ${task.plan.max_records ?? "全库"} 条，最多晋级 ${task.plan.shortlist} 个。批准四阶段依次运行，失败则停止，不自动重试；点券不是美元。AF3/CovDock 不在本次批准范围。`,
          stages: task.plan.steps,
          source_manifest_digest: digest(task.plan.assets),
        },
      },
    })
    // A late reply cannot revive a cancelled/expired/changed plan. Store.approve
    // rechecks all of these after the potentially long native permission wait.
    context.abort.throwIfAborted()
    return store.approve(task.id, task.digest, context.sessionID, context.directory)
  } catch (error) {
    const reason = context.abort.aborted
      ? "资源审批已中断；未批准计算。此草稿已关闭，不会自动重新申请。"
      : Date.now() > task.plan.approve_before
        ? "资源审批已过期；未批准计算。需要重新确认范围与预算。"
        : "未获得有效资源批准，未批准计算；此草稿已关闭。请查看原始审批错误或反馈。"
    try {
      const current = store.endApproval(task.id, task.digest, context.sessionID, context.directory, reason)
      if (current.state === "CANCELLED" && !current.approval)
        context.metadata({
          title: "资源审批未完成，任务已关闭",
          metadata: {
            task_id: task.id,
            digest: task.digest,
            state: current.state,
            inspire_progress: { ...progress(store, current), synced_at: Date.now() },
          },
        })
    } catch {
      // A corrupted plan must not be repaired or labelled safely cancelled.
      store.event(task.id, "approval_cleanup_requires_review", { digest: task.digest })
    }
    throw error // Preserve native rejection/correction semantics for the host.
  }
}
