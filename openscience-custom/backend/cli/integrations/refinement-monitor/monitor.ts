import type { Binding } from "../discovery-platform/binding"
import path from "node:path"
import { mkdir, rename, stat } from "node:fs/promises"
import { candidateTable } from "../../../../frontend/workspace/src/artifacts/candidate-table"
import { refinement, type Refinement, type RefinementRoute } from "../../../../frontend/workspace/src/discovery/refinement-model"

export const root = path.resolve(import.meta.dir, "../../../../..")
const hash = (bytes: string | Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
type Receipt = { id: string; title: string; current: { id: string; filename: string; sha256: string; sourcePath: string } }

export async function snapshot(route: RefinementRoute, base: string, binding: Binding) {
  const values = binding.values.refinement
  const sources: Refinement["sources"] = []
  const read = async (name: string, expected?: string) => {
    const file = path.join(base, name)
    const [bytes, info] = await Promise.all([Bun.file(file).arrayBuffer(), stat(file)])
    const sha256 = hash(new Uint8Array(bytes))
    if (expected && sha256 !== expected) throw Error("Evidence hash mismatch: " + name)
    sources.push({ path: name, sha256, updated: info.mtime.toISOString() })
    return new TextDecoder().decode(bytes)
  }
  const json = async (name: string, expected?: string) => JSON.parse(await read(name, expected))
  const reports: Refinement["reports"] = []
  const receipts = async (name: string, names: string[]) => {
    const receipt = await json(name) as { artifacts: Receipt[] }
    for (const item of receipt.artifacts.filter(item => names.includes(item.current.filename))) {
      const relative = path.relative(binding.sourceRoot, item.current.sourcePath)
      if (relative.startsWith("..") || path.isAbsolute(relative)) throw Error("Report outside registered workspace")
      await read(relative, item.current.sha256)
      reports.push({ id: item.id, version: item.current.id, name: item.current.filename, title: item.title, sha256: item.current.sha256 })
    }
  }
  const common = { schema: "refinement-workbench-v1", route, project: values.v1, target: values.v2 }
  const stage = (id: string, label: string, state: Refinement["stages"][number]["state"], status: string, count: string, title: string, detail: string, facts: string[]) => ({ id, label, state, status, count, title, detail, facts })
  if (route === "library-first") {
    const folder = values.v3
    await receipts(folder + "platform-receipt.json", ["REVIEW.md", "evaluation-summary.json", "library_evaluated_candidates.csv", "v2-consistency-audit.json"])
    const summary = await json(folder + "evaluation-summary.json")
    await read(folder + "v2-consistency-audit.json", summary.audit_sha256)
    const rows = candidateTable(await read(folder + "library_evaluated_candidates.csv", summary.result_table_sha256))?.rows
    if (!rows || rows.length !== summary.candidate_count || rows.length !== 8 || new Set(rows.map(row => row.compound_id)).size !== 8 || rows.some(row => row.route !== route || row.pose_consistency_audit_sha256 !== summary.audit_sha256)) throw Error("Library candidate binding mismatch")
    const agreed = rows.filter(row => row.covdock_gate_detail === "A").length
    const disagreement = rows.filter(row => row.covdock_gate_detail === "B1").length
    if (agreed !== summary.gate_counts.A || disagreement !== summary.gate_counts.B1) throw Error("Library gate counts mismatch")
    return refinement.parse({ ...common, session: values.v4, current: "md",
      notice: "8 个精筛候选全部保留。A 级表示计算姿势一致性，不表示实验活性或已经具备 MD 条件。",
      mdReason: values.v5,
      stats: [{ label: "精筛候选", value: String(rows.length), unit: "个", note: "原始顺序 · 全部保留" },
        { label: "姿势一致性 A", value: String(agreed), unit: "个", note: "通过本轮物理与一致性复核" },
        { label: "构象分歧 B1", value: String(disagreement), unit: "个", note: "保留差异 · 待进一步解释" },
        { label: "正式 MD 轨迹", value: "0", unit: "条已登记", note: "受体与参数准备待完成" }],
      stages: [stage("screen", "库筛选", "done", "已完成", "8 个精筛候选", "库筛选结果已登记", "展示本轮保留下来的 8 个精筛候选，保持原始顺序和分子身份；不把此数目当作全库规模。", ["8 个候选", "化学结构可追溯"]),
        stage("af3", "AF3 预测", "done", "已完成", "8 / 8", "预测构象可查看", "8 个候选的 AF3 原始构象已登记，可与各自 CovDock 构象分别查看。置信指标不等于结合活性。", ["原始 CIF", "逐候选结构"]),
        stage("dock", "共价对接", "done", "已完成", "8 / 8", "CovDock-LO 结果已保留", "展示原始选中姿势、厂商评分与局部应变。结构按候选身份及姿势索引校验后加载。", ["8 个对接结果", "原始评分保留"]),
        stage("review", "一致性复核", "done", "已完成", `${agreed} A · ${disagreement} B1`, "独立构象一致性复核", "以口袋蛋白 Cα 对齐后比较配体，不单独拟合配体。固定原子名称、元素和键级对应；未做对称性最小化。", ["A 级阈值 RMSD ≤ 3 Å", "B1：AF3 / CovDock 分歧"]),
        stage("md", "MD 准备", "current", "待补全受体", "缺失环区", "当前阶段 · 受体完整性与 MD 准备", values.v6, ["正式轨迹未登记", "不复用 De novo 轨迹"]),
        stage("validate", "实验验证", "pending", "待开展", "活性与选择性", "实验验证尚未登记", "目前为计算候选，后续需要独立的实验活性和选择性证据。", ["无实验活性结论"])],
      candidates: rows.map(row => ({ row, label: `候选 ${row.input_sdf_index}`, featured: row.covdock_gate_detail === "A", badge: `Gate ${row.covdock_gate_detail}`,
        tone: row.covdock_gate_detail === "A" ? "teal" : "amber", note: row.covdock_gate_detail === "A" ? "通过本轮物理检查与跨方法姿势一致性门控；进入 MD 前仍需补全受体。" : "物理检查通过，但 AF3 与 CovDock 的配体姿势存在分歧，保留待复核。" })),
      reports, sources, collectedAt: new Date().toISOString(), evidenceUpdatedAt: sources.map(s => s.updated).sort().at(-1) })
  }
  const af3 = values.v7
  const retained = values.v8
  const manifest = await json(af3 + "platform-delivery/manifest.json")
  const audit = await json(af3 + "platform-delivery/AF3_RAW_OUTPUT_AUDIT.json", manifest.files["AF3_RAW_OUTPUT_AUDIT.json"])
  const rows = candidateTable(await read(af3 + "verified-evidence/artifacts/stage4_top_candidates.csv", audit.source_files["artifacts/stage4_top_candidates.csv"]))?.rows
  const geometryManifest = await json(af3 + "topology-evidence/geometry-delivery-manifest.json")
  const geometry = await json(af3 + "topology-evidence/local-geometry-review.json", geometryManifest.files["local-geometry-review.json"])
  const publication = await json(retained + values.v9)
  const evidence = await json(retained + values.v10, publication.files[values.v11])
  const descriptorHash = Object.entries(evidence.comparison.sources).find(([name]) => name.endsWith("/covdock_lo_registered_manifest.json"))?.[1]
  if (typeof descriptorHash !== "string") throw Error("Missing retained pose binding")
  const descriptor = await json(retained + values.v12, descriptorHash)
  const terminal = await json(retained + "terminal-artifact-manifest.json")
  const registration = await json(values.v13) as { structures: { compound: string; method: string; artifact: Receipt }[] }
  if (!rows || rows.length !== 3 || rows.map(row => row.compound_id).sort().join() !== [values.v14, values.v15, values.v16].join() || audit.records.length !== 3 || evidence.original_candidate_count !== 3 || evidence.readback_candidate_count !== 1 || evidence.full_workflow_complete || terminal.state !== "TIMED_OUT" || terminal.state !== evidence.parent_state || descriptor.parent_run_id !== terminal.run_id || descriptor.retained_pose_count !== 10 || evidence.selected.compound_id !== values.v17 || evidence.selected.covdock_pose_index !== "7") throw Error("Ligand evidence coverage mismatch")
  const candidates = rows.map(row => {
    const verified = audit.records.find((record: { compound_id: string }) => record.compound_id === row.compound_id)
    const local = geometry.rows.find((record: { compound_id: string }) => record.compound_id === row.compound_id)
    if (!verified || local?.model_sha256 !== verified.model_sha256 || verified.actual_sample_count !== 1) throw Error("AF3 audit identity mismatch")
    const refs = Object.fromEntries(registration.structures.filter(ref => ref.compound === row.compound_id).flatMap(ref => {
      if (ref.method === "af3" && ref.artifact.current.sha256 !== verified.model_sha256) throw Error("AF3 structure registration mismatch")
      if (ref.method === "covdock" && (ref.compound !== evidence.selected.compound_id || ref.artifact.current.sha256 !== descriptor.pose_sha256)) throw Error("CovDock structure registration mismatch")
      return [[`${ref.method}_artifact_id`, ref.artifact.id], [`${ref.method}_artifact_version`, ref.artifact.current.id], [`${ref.method}_artifact_sha256`, ref.artifact.current.sha256]]
    }))
    const selected = row.compound_id === evidence.selected.compound_id
    return { row: { ...row, route, ...refs, af3_mpae: String(verified.mpae), mw: row.molecular_weight,
      af3_geometry_dihedral: String(local.SG_C1_C2_C3_dihedral_degrees), actual_sample_count: "1",
      ...(selected ? { ...evidence.selected, covdock_structure_pose: evidence.selected.covdock_pose_index,
        af3_covdock_ligand_rmsd_angstrom: String(evidence.comparison.metrics.af3_covdock_ligand_rmsd_angstrom) } : {}) },
      label: row.compound_id.split("_").at(-1), featured: selected, badge: selected ? "对接结果已保留" : "对接待完成", tone: selected ? "blue" : "amber",
      note: selected ? values.v18 : row.compound_id.endsWith(values.v19) ? values.v20 : values.v21 }
  })
  await receipts(af3 + "platform-delivery/platform-receipt.json", ["LIGAND_AF3_REPORT.md", "AF3_AUDITED_SUMMARY.csv", "AF3_RAW_OUTPUT_AUDIT.json"])
  await receipts(af3 + "topology-evidence/geometry-platform-receipt.json", ["AF3_LOCAL_GEOMETRY_REVIEW.md"])
  await receipts(retained + values.v22, [values.v23, values.v24])
  return refinement.parse({ ...common, session: values.v25, current: "dock",
    notice: values.v26,
    mdReason: values.v27,
    stats: [{ label: "共价化候选", value: "3", unit: "个", note: values.v28 },
      { label: "AF3 预测", value: "3", unit: "/ 3", note: "每候选 1 个实际样本 · 几何待复核" },
      { label: "已保留对接结果", value: String(evidence.readback_candidate_count), unit: "/ 3", note: values.v29 },
      { label: "最终评价 / MD", value: "待完成", unit: "", note: "尚无最终排名及 MD 轨迹" }],
    stages: [stage("design", "母体设计", "done", "已登记", values.v30, "从已知配体开展共价化设计", values.v31, ["3 个本轮候选", "丙炔酰胺弹头"]),
      stage("af3", "AF3 预测", "done", "结果已验收", "3 / 3", "三候选预测已完成", "真实输出每候选只有 1 个样本，按实际产物展示。运行、身份和连接已核验，局部加合物几何仍有明确限制。", ["实际样本 1 / 候选", values.v32]),
      stage("dock", "共价对接", "blocked", "超时 · 部分保留", "1 / 3 可查看", "当前阶段 · 对接结果恢复与补全", values.v33, [values.v34, "原任务 TIMED_OUT", "未发起新的计算"]),
      stage("review", "独立评价", "pending", "待完整结果", "无最终排名", "保留证据边界后再评价", values.v35, [values.v36, "三候选分母不变"]),
      stage("md", "MD 验证", "pending", "未开展", "0 条已登记", "等待候选评价与参数验收", "尚无本路线候选的正式生产轨迹。后续轨迹需绑定本路线、候选和重复身份，才能进入自动分析。", ["无 RMSD / RMSF 数据", "无轨迹图表"]),
      stage("validate", "实验验证", "pending", "待开展", "活性与选择性", "实验数据尚未登记", "计算设计候选需要后续实验活性与选择性验证。", ["无实验活性结论"])],
    candidates, reports, sources, collectedAt: new Date().toISOString(), evidenceUpdatedAt: sources.map(s => s.updated).sort().at(-1) })
}

