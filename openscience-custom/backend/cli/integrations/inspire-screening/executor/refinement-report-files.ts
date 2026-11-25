import { canonical } from "./model"
import type { combineRefinementReport } from "./refinement-report"
import { refinementPareto } from './refinement-pareto'

type Report = ReturnType<typeof combineRefinementReport>
const cell = (value: unknown) => String(value ?? "").replace(/[\r\n]+/g, " ").replace(/[&<>|`*_\[\]\\]/g,
  (character) => `&#${character.charCodeAt(0)};`)
const csv = (value: unknown) => {
  const text = value === null || value === undefined ? "" : String(value)
  // Quoting alone does not stop formula execution in spreadsheet applications.
  const safe = typeof value === "string" && /^[\s]*[=+@-]/.test(text) ? "'" + text : text
  return '"' + safe.replace(/"/g, '""') + '"'
}

// Pure rendering of the independently validated report; no model narration,
// substituted score, dropped rejection, execution or implicit publication.
export function refinementReportFiles(report: Report): Record<string, string> {
  const strict = report.quality_policy?.version === 'refinement-structural-qc-v2-provisional'
  const ranking = report.final_ranking === 'af3-strain-pareto-v1' ? refinementPareto(report.candidates.map((row) => ({ ...row,
    covdock: { measurements: { strain_kcal_mol: row.covdock.measurements?.engine === 'covdock' ? row.covdock.measurements.strain_kcal_mol : null } } }))) : null
  const extra: Record<string, string> = ranking ? {
    'final-pareto.json': JSON.stringify({ workflow_id: report.workflow_id, workflow_digest: report.workflow_digest,
      input_sha256: report.input_sha256, code: report.code, ...ranking }, null, 2) + '\n',
    'final-pareto.csv': [['input_sdf_index', 'compound_id', 'pareto_front', 'display_order', 'eligible', 'af3_mpae', 'strain_kcal_mol', 'source_status'],
      ...ranking.candidates.map((r) => [r.input_sdf_index, r.compound_id, r.pareto_front, r.display_order, r.eligible, r.af3_mpae, r.strain_kcal_mol, r.source_status])]
      .map((row) => row.map(csv).join(',')).join('\r\n') + '\r\n',
  } : {}
  const header = ["input_sdf_index", "compound_id", "selection_channel", "carsidock_rank_fraction", "rtmscore_rank_fraction", "rank_disagreement",
    "combined_structural_status", "af3_execution", "af3_mpae", "af3_quality", "af3_rejection", "af3_failure",
    "covdock_execution", "covdock_score", "covdock_quality", "covdock_rejection", "covdock_failure",
    ...(strict ? ['af3_physical_validity', 'af3_evidence_complete', 'af3_review_eligible',
      'covdock_physical_validity', 'covdock_evidence_complete', 'covdock_review_eligible',
      'covdock_strain_source', 'covdock_strain_measurement_status', 'calibration_status'] : [])]
  const rows = report.candidates.map((row) => [row.input_sdf_index, row.compound_id, row.selection_channel,
    row.carsidock_rank_fraction, row.rtmscore_rank_fraction, row.rank_disagreement, row.status,
    row.af3.status, row.af3.score, row.af3.quality.status, row.af3.quality.reason, row.af3.failure_reason,
    row.covdock.status, row.covdock.score, row.covdock.quality.status, row.covdock.quality.reason, row.covdock.failure_reason,
    ...(strict ? [row.af3.quality.assessment?.physical_validity, row.af3.quality.assessment?.evidence_complete,
      row.af3.quality.assessment?.review_eligible, row.covdock.quality.assessment?.physical_validity,
      row.covdock.quality.assessment?.evidence_complete, row.covdock.quality.assessment?.review_eligible,
      row.covdock.measurements?.engine === 'covdock' ? row.covdock.measurements.strain_provenance?.source_field : null,
      row.covdock.measurements?.engine === 'covdock' ? row.covdock.measurements.strain_provenance?.status : null, 'provisional'] : [])])
  const markdown = ["# EGFR C797：AF3 / CovDock 精筛综合报告", "",
    `工作流：${cell(report.workflow_id)}；来源主筛：${cell(report.parent_task_id)}。`, "",
    `候选总数：${report.candidate_count}。所有候选、拒绝和失败均保留。`, "",
    ...Object.entries(report.interpretation).map(([name, value]) => `- ${cell(name)}：${cell(value)}`), "",
    "## 冻结身份与质量规则", "",
    `工作流 SHA256：${report.workflow_digest}`, "", `输入 SHA256：${report.input_sha256}`, "",
    `分子身份 SHA256：${report.input_identity_sha256}`, "",
    "```json", JSON.stringify(canonical(report.quality_policy), null, 2), "```", "",
    "## 逐候选结果", "", "|原始 ID / SDF 索引|选择通道 / 排名分歧|AF3 状态 / mPAE / 质检|CovDock 状态 / 评分 / 质检|综合结构结论|",
    "|---|---|---|---|---|",
    ...report.candidates.map((row) => `|${cell(row.compound_id)} / ${row.input_sdf_index}|${cell(row.selection_channel)} / ${row.rank_disagreement}|${row.af3.status} / ${cell(row.af3.score)} / ${row.af3.quality.status}|${row.covdock.status} / ${cell(row.covdock.score)} / ${row.covdock.quality.status}|${row.status}|`), "",
    ...(strict ? ["## 独立评价轴", "", "未准入不等于结构失败；UNKNOWN 表示证据不足。下表准入仅用于复核，不授予计算或 MD 权限。", "",
      "|候选|引擎|物理有效性|证据完整|复核准入|", "|---|---|---|---|---|",
      ...report.candidates.flatMap((row) => [row.af3, row.covdock].map((engine) =>
        `|${cell(row.compound_id)}|${engine.engine}|${cell(engine.quality.assessment?.physical_validity)}|${cell(engine.quality.assessment?.evidence_complete)}|${cell(engine.quality.assessment?.review_eligible)}|`)), ""] : []),
    "## 拒绝与失败详情", "",
    ...report.candidates.flatMap((row) => [row.af3, row.covdock].filter((engine) => engine.failure_reason || engine.quality.reason)
      .map((engine) => `- ${cell(row.compound_id)} / ${engine.engine}：${cell(engine.failure_reason ?? engine.quality.reason)}`)), "",
    "## 原始证据", "", "每个原始文件的 SHA256、远端路径、测量和结构索引见同目录 JSON；远端路径不是浏览器下载链接。", "",
    "此报告仅表示固定结构评估与质检结果，不确认亲和力、实验活性或命中。", ""].join("\n")
  return { ...extra, "refinement-report.md": markdown + (ranking ? '\n## 最终计算优先级\n\n见 final-pareto.csv / final-pareto.json。' + ranking.note + '\n' : report.final_ranking === 'library-first-pareto-v2.0'
    ? '\n## 最终计算优先级\n\n完整交付后，由科研端 library-first-pareto-v2.0 生成 final-pareto.csv/json 和 candidate-results.csv；交付完成前不得将本表顺序当作最终排名。缺测和淘汰者仍保留，未具备完整目标者不强行排名。\n' : ''), "refinement-report.csv": [header, ...rows].map((row) => row.map(csv).join(",")).join("\r\n") + "\r\n",
    "refinement-report.json": JSON.stringify(canonical(report), null, 2) + "\n" }
}
