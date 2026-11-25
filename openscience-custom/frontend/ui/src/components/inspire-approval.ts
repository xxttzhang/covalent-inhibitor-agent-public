const display = (value: unknown) => typeof value === "string" || typeof value === "number" ? String(value) : "未提供"

export function inspireDetails(plan: Record<string, unknown>): [string, string][] {
  const stages = Array.isArray(plan.stages) ? plan.stages.filter((value): value is Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value)) : []
  const resources = stages.map((stage) => ({ phase: display(stage.phase), minutes: display(stage.minutes),
    resource: stage.resource !== null && typeof stage.resource === "object" ? stage.resource as Record<string, unknown> : {} }))
  const field = (name: string, fallback: unknown) => resources.length
    ? resources.map((stage) => `${stage.phase}: ${display(stage.resource[name])}`).join("；")
    : display(fallback)
  const points = typeof plan.maximum_compute_points === "number" && Number.isFinite(plan.maximum_compute_points)
    ? plan.maximum_compute_points.toFixed(3) : plan.maximum_compute_points === null && plan.budget_policy === "uncapped-test100-v1"
      ? "不设金额上限（100候选测试；仍受单GPU和分阶段时限限制）" : "未提供"
  const selected = Array.isArray(plan.selected_candidates) ? plan.selected_candidates : []
  const delivery = plan.delivery_policy && typeof plan.delivery_policy === "object"
    ? plan.delivery_policy as Record<string, unknown> : undefined
  const channels: Record<string, string> = { rank_disagreement_review: "排名分歧复核", consensus_sentinel: "一致性对照", diversity_sentinel: "多样性对照" }
  const candidates = selected.length > 0 && selected.length <= 100 && selected.every((row) => row && typeof row === "object" &&
    typeof row.compound_id === "string" && Number.isSafeInteger(row.input_sdf_index) && row.input_sdf_index >= 0 &&
    typeof row.selection_channel === "string" && channels[row.selection_channel] &&
    typeof row.rank_disagreement === "number" && Number.isFinite(row.rank_disagreement) && row.rank_disagreement >= 0 && row.rank_disagreement <= 1)
    ? selected.map((row) => `${row.compound_id} [${row.input_sdf_index}]：${channels[row.selection_channel]}；排名分歧 ${row.rank_disagreement.toFixed(3)}`).join("\n")
    : "候选名单缺失或无效；请勿批准"
  const refinement: [string, string][] = plan.parent_task_id ? [
    ['最终排名与总览', delivery?.final_ranking === 'library-first-pareto-v2.0'
      ? '完整交付后本地复用科研 Pareto v2：mPAE↓、属性适配↑、化学风险↓、局部应变↓、构象 RMSD↓；完整数值才可排名，同层按骨架多样性及既定规则排序。失败/缺测保留；另交付候选结构总览。不是实验活性证明。'
      : delivery?.final_ranking === 'af3-strain-pareto-v1' ? '历史双指标 Pareto（mPAE↓、应变↓），不是完整科研排名。'
        : '本审批未包含最终 Pareto 排名或候选总览，不追加到旧审批。'],
    ["精筛来源任务", display(plan.parent_task_id)],
    ["冻结精筛名单 / 理由", candidates],
    ["选择清单校验", display(plan.selection_manifest_digest)],
    ["输入文件校验", display(plan.input_sha256)],
    ["候选身份校验", display(plan.identity_sha256)],
    ["精筛引擎参数", plan.engine_parameters && typeof plan.engine_parameters === "object" ? JSON.stringify(plan.engine_parameters) : "未提供"],
    ["冻结科学质检规则", plan.scientific_quality_policy && typeof plan.scientific_quality_policy === "object"
      ? `${JSON.stringify(plan.scientific_quality_policy)}；通过只表示所列结构质检通过，不证明亲和力或活性；未验证完整拓扑/立体化学。AF3 置信度描述性报告，缺少必要测量不得通过。`
      : "本审批没有新版结构质检政策；不能据此声称按新规则验收。"],
    ["精筛验收计数口径", plan.result_contract === "screening-refinement-outcome-v2"
      ? "分别统计引擎成功、计算失败、质量通过和质量淘汰。覆盖率按引擎成功计算；可以全部淘汰，但每个结论必须有独立验收证据；通过不等于实验证实活性。"
      : plan.result_contract === "screening-refinement-outcome-v1" || plan.result_contract === undefined
        ? "历史结果口径，未单独记录质量淘汰；不会自动套用新规则。"
        : "未知验收口径；请勿批准"],
    ["精筛代码版本", plan.code_release && typeof plan.code_release === "object" ? JSON.stringify(plan.code_release) : "未提供"],
    ["精筛运行资产", plan.runtime_assets && typeof plan.runtime_assets === "object" ? JSON.stringify(plan.runtime_assets) : "未提供"],
    ["精筛审批说明", "独立新增预算；排名分歧不是校准的不确定性，对照不是不确定候选；不继承核心筛选的批准"],
    ...(plan.runtime_admission === "cpu-before-approval-gpu-at-start-v1" ? [["作业内运行检查",
      "审批前检查 CPU 权重及正式授权；批准后在 AF3 作业内检查单 GPU、参数加载和设备运算，CovDock 启动前再检查授权。计入所列时限；失败停止，不自动重试，不视为结构评估完成。"] as [string, string]] : []),
    ...(plan.workflow_contract ? [
      ["一次确认的执行顺序", plan.workflow_contract === "screening-refinement-workflow-v1" && plan.execution === "af3_then_covdock_same_subset"
        ? "AF3 结构评估核验完成 → CovDock 独立共价对接同一候选名单；不自动沿用 AF3 构象，不扩充或替换候选。"
        : "未知流程合同或执行顺序；请勿批准"],
      ["失败与质量淘汰", plan.failure_policy === "stop_on_execution_failure" && plan.quality_policy === "retain_rejections_and_continue"
        ? "执行失败停止后续提交，不自动重试；质量淘汰保留并继续对同一名单进行独立评估。AF3 置信度不是亲和力或活性验证。"
        : "未知失败或质量策略；请勿批准"],
      ["分阶段新增预算", stages.map((stage) => `${display(stage.phase)}: ${display(stage.maximum_compute_points)} 点券`).join("；")],
      ["精筛自动文件交付", delivery?.mode === "all-refinement-artifacts-v1"
        ? `${JSON.stringify(delivery)}；两阶段完成且原文件核验后，自动保存综合报告和完整精筛输出清单到 Results。超限暂停，不截断或重跑计算；Results 副本和临时文件另占空间。`
        : "本次审批未包含精筛自动交付；不会继承主筛或追加到旧审批。"],
    ] as [string, string][] : []),
  ] : []
  const core: [string, string][] = !plan.parent_task_id && plan.stages ? [
    ["靶点 / 位点", display(plan.target)],
    ["候选库", display(plan.library)],
    ...(typeof plan.preparation_cohort === "string" ? [["测试候选要求", plan.preparation_cohort] as [string, string]] : []),
    ["源库读取范围", plan.source_scope === "full" && plan.max_records === null ? "全库" :
      plan.source_scope === "bounded" && typeof plan.max_records === "number" && Number.isSafeInteger(plan.max_records) && plan.max_records > 0
        ? `有界读取，最多 ${plan.max_records} 条` : "未提供或无效；请核对冻结计划"],
    ["准备阶段随机种子", display(plan.seed)],
    ["质检门槛", plan.quality_gates && typeof plan.quality_gates === "object"
      ? JSON.stringify(plan.quality_gates) : "未提供；请核对冻结计划"],
    ["自动文件交付", plan.delivery_policy && typeof plan.delivery_policy === "object"
      ? `${JSON.stringify(plan.delivery_policy)}；完成后自动保存完整核心文件清单，超限暂停不截断。上限按文件内容计，Results 副本和临时文件另占本地空间。`
      : "本计划未包含自动交付；不会追加到旧审批"],
    ["失败时的交付", delivery?.failure === "diagnostics-and-verified-prefix-v1"
      ? "失败后自动保存已验证的上游文件与白名单诊断文件；沿用本次文件数/字节上限，不交付未核验结构，不重跑计算。日志可能含运行信息，将保存在本项目 Results。"
      : "未批准自动交付失败文件；不追加到旧审批"],
  ] : []
  return [
    ["任务编号", display(plan.task_id)],
    ["平台 / 项目", `Inspire · ${display(plan.project)}`],
    ["账号 / 文件入口", `${display(plan.account)} / ${display(plan.notebook)}`],
    ["资源空间", field("workspace", plan.workspace)],
    ["计算组", field("group", plan.group)],
    ["规格 GPU,CPU,GiB", field("quota", plan.quota)],
    ["候选数 / 总时限", `${display(plan.candidates)} 个 / ${display(plan.minutes)} 分钟`],
    ["分阶段时限", resources.length ? resources.map((stage) => `${stage.phase}: ${stage.minutes} 分钟`).join("；") : display(plan.minutes) + " 分钟"],
    ["实时计价", field("points_per_hour", plan.points_per_hour) + " 点券/小时（各单实例）"],
    ["批准点券上限", `${points} 点券；按资源时限预留，非账单保证`],
    ["受体位点", display(plan.structure_site)],
    ["镜像", field("image", plan.image)],
    ["冻结计划", display(plan.digest)],
    ...core,
    ...refinement,
  ]
}
