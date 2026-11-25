import type { StudyOverview } from "./discovery-study"
import { hypothesisDisplayReport } from "./discovery-hypothesis-display"

/** Display original handoff blockers without interpreting them as execution results. */
export function hypothesisBlockers(handoff: Record<string, unknown>) {
  const intake = handoff.intake && typeof handoff.intake === "object"
    ? handoff.intake as Record<string, unknown> : {}
  const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
  return [...new Set([
    ...strings(intake.missing).map(item => `缺少输入：${item}`),
    ...strings(handoff.compatibility_blockers),
    ...strings(handoff.unresolved),
    ...(typeof handoff.plan_error === "string" ? [handoff.plan_error] : []),
  ])]
}

/** A descriptive evidence report, never a claim of a newly executed experiment. */
export function studyReport(view: StudyOverview) {
  const p = view.plan
  const hypotheses = view.hypothesisAttachments ?? (view.hypothesisAttachment ? [view.hypothesisAttachment] : [])
  return ["统一研究证据报告", p.title, `研究 ID：${p.id}`, `创建时间：${p.createdAt}`,
    `靶点：${p.target.gene} / ${p.target.uniprot}；物种：${p.target.taxon}`,
    `研究位点：${p.target.site.chain}:${p.target.site.residue}${p.target.site.number}:${p.target.site.atom}（${p.target.site.numbering}）`,
    `计划受体：${p.target.receptor.reference}`,
    ...(view.libraryPlan ? [`Library-first 计划编译时间：${view.libraryPlan.createdAt}；SHA256：${view.libraryPlan.planDigest}`,
      "该计划未批准、未提交；执行前必须重新核验资源并走原生审批。", JSON.stringify(view.libraryPlan.plan, null, 2)] : []),
    "注意：计划受体不代表历史任务实际使用的受体；实际结构须依据各历史产物及来源核对。",
    ...(view.ligandPlan ? [`Ligand-first 上游编译计划接入时间：${view.ligandPlan.createdAt}；SHA256：${view.ligandPlan.planDigest}`,
      "仅核对上游编译快照，未批准、未提交；远端资产与执行条件仍需复核。", JSON.stringify(view.ligandPlan.plan, null, 2)] : []),
    ...(view.denovoPlan ? [`De novo 本地计划编译时间：${view.denovoPlan.createdAt}；SHA256：${view.denovoPlan.planDigest}`,
      "只校验本地注册包；未批准、未提交，远端资产与实时资源尚待核验。", JSON.stringify(view.denovoPlan.plan, null, 2)] : []),
    `科学假设状态：${p.hypothesis.state}`, p.hypothesis.rationale || "尚未提供科学假设",
    ...p.hypothesis.evidence.map(e => `假设证据：${e.reference}；SHA256：${e.sha256 ?? "未登记"}`),
    ...(view.hypothesisReuses ?? []).flatMap(item => ["", "历史推理已显式复用（仅证据，不可直接提交计算）",
      `来源研究：${item.sourcePlan.title}；${item.sourcePlan.id}`,
      `原受体：${item.sourcePlan.target.receptor.reference}；当前计划受体：${p.target.receptor.reference}。结构差异必须另行核验。`,
      `路线：${item.route}；来源：${item.source}；SHA256：${item.sha256}；接入时间：${item.importedAt}`,
      ...hypothesisDisplayReport(item.original), "原始证据（保留原研究 ID）：", item.original]),
    ...(hypotheses.length ? hypotheses.flatMap(item => ["", "上游假设正式交接（非计算完成证明）",
      `来源：${item.source}；SHA256：${item.sha256}`, `接入时间：${item.importedAt}`,
      `路线：${item.value.hypothesis.route}；原始状态：${item.value.handoff.state}`,
      `执行绑定：${item.executionBinding}`,
      ...hypothesisDisplayReport(item.original),
      ...(item.contractValidation ? [`实际执行接口校验：${item.contractValidation.state}（不代表编译或提交）`, ...item.contractValidation.blockers] : []),
      ...hypothesisBlockers(item.value.handoff),
      "原始假设与交接信息：", item.original,
    ]) : [view.hypothesisReuses?.length ? "本次新生成假设尚未接入；上述为显式复用的历史证据，不是本次执行交接。" : "未接入上游假设产物；计划中的文字依据不是执行交接。"]),
    "", "本次报告读取未提交新科研计算。既有计算须依据任务记录核验；历史导入成功不等于原流程全部完成，也不证明候选活性。",
    "执行准备、提交、状态查询和失败恢复尚须按独立验收表检查；输入声明不能替代真实执行。",
    ...view.routes.flatMap(route => ["", `路线：${route.route}；接入状态：${route.state}`, route.reason,
      ...(route.missing.length ? [`缺少输入：${route.missing.join("、")}`] : []),
      ...(route.imported ? ["历史结果复用", `原运行：${route.imported.receipt.source.run}`,
        route.imported.receipt.targetIdentity
          ? `导入身份已核验并冻结：${JSON.stringify(route.imported.receipt.targetIdentity)}`
          : "旧导入回执未记录完整物种/UniProt/编号身份；未事后补写或假定已核验。",
        `证据更新时间（不是计算耗时）：${route.imported.receipt.source.evidenceUpdatedAt}`,
        `导入时间：${route.imported.receipt.importedAt}`, `来源：${route.imported.receipt.source.path}`,
        `SHA256：${route.imported.receipt.source.sha256}`,
        ...route.imported.data.stages.map(s => `原阶段 ${s.label}：${s.state} / ${s.status}；${s.detail}`),
        ...route.imported.data.sources.map(s => `原证据：${s.path}；SHA256：${s.sha256}；时间：${s.updated}`),
      ] : ["未导入任何候选。"]),
    ]), "", `候选池：${view.pool.groups.length} 条身份记录，${view.pool.sourceCount} 条原始来源`,
    `身份策略：${view.pool.policy}；RDKit：${view.pool.rdkitVersion ?? "不可用，未合并"}`,
    "不混排不同方法原始分数；未提供的指标不是零；未确定分子身份的记录独立保留。",
    ...view.pool.groups.flatMap(group => ["", `身份记录：${group.id}（${group.identity.state}）`,
      `SMILES：${group.identity.canonical ?? group.sources[0].candidate.smiles}`, group.identity.reason,
      ...group.sources.flatMap(source => [`来源路线：${source.route}；原运行：${source.sourceRun}；候选：${source.candidate.id}`,
        source.candidate.note,
        ...(source.candidate.sourceAssessment ? [
          `原始 QC 回执：${source.candidate.sourceAssessment.producerVerdict}；解释状态：${source.candidate.sourceAssessment.status}；评价过程：${source.candidate.sourceAssessment.evaluationState}。`,
          `失败记录：${source.candidate.sourceAssessment.failures.join("；") || "无"}；证据缺口：${source.candidate.sourceAssessment.missingReasons.join("；") || "无"}。`,
          `回执来源：${source.candidate.sourceAssessment.evidence.path}；SHA256：${source.candidate.sourceAssessment.evidence.sha256}；仅核验回执一致性，未重新计算物理质量或活性。`,
        ] : []),
        ...source.candidate.metrics.map(m => `${m.label}：${m.value === null ? "未提供" : m.value} ${m.unit}；定义：${m.definition}`),
        ...source.candidate.structures.map(ref => `结构：${ref.method} / ${ref.format}；${ref.asset.storage === "artifact" ? ref.asset.id + " / " + ref.asset.version : ref.asset.path}；SHA256：${ref.asset.sha256}；姿势索引：${ref.pose ?? "未登记"}`),
      ]),
    ]),
  ].join("\n")
}
