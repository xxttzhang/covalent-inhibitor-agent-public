/** Present recorded evidence only. This does not verify a scientific claim. */
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
const text = (value: unknown) => typeof value === "string" ? value : ""
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : []

export function evidenceURL(value: unknown) {
  const parsed = URL.canParse(text(value)) ? new URL(text(value)) : null
  return parsed && ["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : undefined
}

export function hypothesisDisplay(original: string) {
  const payload = record(JSON.parse(original))
  const hypothesis = record(payload.hypothesis)
  const target = record(payload.target)
  const binding = record(record(payload.campaign_registration).input_binding)
  const pose = record(binding.parent_pose)
  const handoff = record(payload.handoff)
  const intake = record(handoff.intake)
  const references = list(payload.references).map(record).map(item => ({
    id: text(item.id), title: text(item.title) || text(item.id) || "未提供标题",
    url: evidenceURL(item.url),
  }))
  const claims = list(hypothesis.claims).map(record).map(item => ({
    statement: text(item.statement), interpretation: text(item.interpretation),
    evidence: list(item.evidence_ids).filter((id): id is string => typeof id === "string").map(id => ({
      id, reference: references.find(reference => reference.id === id),
    })),
  }))
  const missing = [
    ...list(intake.missing).map(item => typeof item === "string" ? `缺少输入：${item}` : ""),
    ...list(hypothesis.uncertainties), ...list(handoff.compatibility_blockers), ...list(handoff.unresolved), handoff.plan_error,
  ].filter((item): item is string => typeof item === "string" && !!item)
  const site = [
    `原始靶点：${text(target.symbol) || "未提供"} / ${text(target.uniprot_id) || "未提供"}；物种：${target.taxon ?? "未提供"}`,
    `原始位点：${text(target.site) || "未提供"}`,
    `结构依据：${text(pose.pdb_id) || "原始绑定未提供 PDB"}；链：${text(pose.protein_chain_id) || "未提供"}`,
    `声明的编号映射：结构 ${pose.target_author_residue_number ?? "未提供"} → 标准 ${pose.target_uniprot_residue_number ?? "未提供"}（映射声明不等于已验证）`,
  ]
  return { rationale: text(hypothesis.rationale) || "原始产物未提供靶点依据", claims, references, site,
    route: text(hypothesis.route) || "未提供", missing: [...new Set(missing)],
    warning: "以下为原始科学假设与证据关联，未经此展示层独立验证；不代表实验结论或执行许可。" }
}

export function hypothesisDisplayReport(original: string) {
  const view = hypothesisDisplay(original)
  return [view.warning, "靶点依据", view.rationale, "文献证据与支持判断",
    ...view.claims.flatMap(claim => [claim.statement, ...claim.evidence.map(item =>
      `${item.id}：${item.reference?.title ?? "未提供参考文献记录"}；${item.reference?.url ?? "未提供安全可用链接"}`)]),
    ...view.references.map(item => `参考文献：${item.id}；${item.title}；${item.url ?? "未提供安全可用链接"}`),
    "位点选择与结构依据", ...view.site, `原始路线建议：${view.route}`,
    "路线理由见上述原始依据；未提供跨路线比较时不推断优劣。", "当前限制",
    ...(view.missing.length ? view.missing : ["原始产物未列出限制，不代表不存在限制。"])]
}
