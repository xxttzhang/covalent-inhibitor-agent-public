import path from "node:path"
import { z } from "zod"
import { result, type DiscoveryResult } from "@synsci/util/discovery-result"
import { mdMetricDefinitions } from "@synsci/util/metric-semantics"
import type { Refinement } from "../../../../frontend/workspace/src/discovery/refinement-model"
import type { Binding } from "./binding"

const hash = (bytes: string) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
const manifest = z.object({ status: z.literal("REPLICA_METRICS_BOUND"), candidate: z.string(), replica: z.number().int(), frames: z.literal(1000), production_ps: z.literal(10000),
  input_manifest_sha256: z.string(), initial_velocity_file_sha256: z.string(), CPU_smoke_only: z.literal(false), convergence_established: z.literal(false), affinity_or_kinact_proven: z.literal(false), files: z.record(z.string(), z.string()) })
const metricMeans = z.object({ whole_ligand_RMSD_A: z.number().finite(), largest_ring_system_RMSD_A: z.number().finite(), ligand_heavy_COM_displacement_A: z.number().finite(), initial_contact_retention_excluding_tether: z.number().finite().min(0).max(1) })
const fileVerification = z.object({ candidate: z.string(), replica: z.number().int(), passed: z.literal(true), metrics_means: metricMeans,
  input_manifest_sha256: z.string(), analysis_manifest_sha256: z.string(), initial_velocity_file_sha256: z.string() }).passthrough()
const dockingEvidence = z.object({ schema: z.literal("ligand-first-docking-platform-v1"), completed_at: z.string().datetime(), covdock_run: z.string(), evaluation_run: z.string(), pareto_run: z.string(), source_table_sha256: z.string(),
  candidates: z.array(z.object({ compound_id: z.string(), system_id: z.string(), rank: z.number().int(), gate: z.literal("B2"), covdock_score: z.number(), local_strain_energy: z.number(), pose: z.number().int(), covdock_output_sha256: z.string().regex(/^[a-f0-9]{64}$/) })) })
const chartManifest = z.object({ schema: z.literal("ligand-first-md-charts-v1"), source_archive_sha256: z.string().regex(/^[a-f0-9]{64}$/), source_snapshot_sha256: z.string().regex(/^[a-f0-9]{64}$/), convergence_established: z.literal(false), affinity_or_activity_proven: z.literal(false), charts: z.array(z.object({
  id: z.string(), candidate: z.string(), system: z.string(), kind: z.string(), title: z.string(), caption: z.string(), project_path: z.string(), source_path: z.string(), sha256: z.string().regex(/^[a-f0-9]{64}$/), media_type: z.literal("image/svg+xml"), x_unit: z.string(), y_unit: z.string(), replicas: z.array(z.number().int().min(1).max(3)).min(1),
})) })
const snapshot = z.object({ observed_epoch: z.number(), scheduler: z.object({ items: z.array(z.object({ name: z.string(), status: z.string(), project: z.string() })), unresolved: z.array(z.unknown()) }),
  scientific_outputs: z.record(z.string(), z.object({ "status.json": z.object({ system_id: z.string(), state: z.string(), analysis_complete: z.boolean(), active_replica: z.number().optional(), replicas: z.array(z.object({ replica: z.number(), production_ps: z.number(), frames: z.number(), analysis_manifest_sha256: z.string() })) }), replicas: z.record(z.string(), z.object({
    "finished.json": z.object({ candidate: z.string(), replica: z.number(), status: z.string(), production_ps: z.number().optional(), production_frames: z.number().optional(), smoke_only: z.boolean(), manifest_sha256: z.string(), initial_velocity_file_sha256: z.string().optional() }).optional(),
    "short-qc.json": z.object({ status: z.string() }).optional(),
    "energy-force-check.json": z.object({ passed: z.boolean() }).optional(),
    analysis_manifest_text: z.string().optional(),
    analysis_audit_text: z.string().optional(),
    analysis_log_tail: z.string().optional(),
    file_verification: fileVerification.optional(),
  })) })) })

const mean = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length
const sampleSD = (values: number[]) => values.length > 1 ? Math.sqrt(values.reduce((sum, value) => sum + (value - mean(values)) ** 2, 0) / (values.length - 1)) : undefined
const aggregateMetrics = (replicas: z.infer<typeof fileVerification>[]) => {
  return mdMetricDefinitions("ligand-first", replicas.length).map(entry => {
    const values = replicas.map(replica => replica.metrics_means[entry.key] * entry.scale)
    const sd = sampleSD(values)
    return { id: entry.id, label: entry.label, value: mean(values), unit: entry.unit, definition: entry.definition,
      ...(sd === undefined ? {} : { uncertainty: { kind: "SD" as const, value: sd, independentN: values.length } }) }
  })
}

export async function ligandMD(packet: DiscoveryResult, original: Refinement, base: string, settings: NonNullable<Binding["ligandMD"]>) {
  if (packet.route !== "ligand-first" || packet.run !== settings.run || packet.project !== settings.project) throw Error("Ligand MD scope mismatch")
  const bytes = await Bun.file(path.join(base, settings.source)).text()
  const sha256 = hash(bytes)
  if (sha256 !== settings.sha256) throw Error("Ligand MD snapshot hash mismatch")
  const data = snapshot.parse(JSON.parse(bytes))
  const dockingBytes = await Bun.file(path.join(base, settings.dockingSource)).text()
  const dockingSha256 = hash(dockingBytes)
  if (dockingSha256 !== settings.dockingSha256) throw Error("Ligand docking evidence hash mismatch")
  const docking = dockingEvidence.parse(JSON.parse(dockingBytes))
  let chartPackage: z.infer<typeof chartManifest> | undefined
  if (settings.chartsSource && settings.chartsSha256) {
    const chartBytes = await Bun.file(path.join(base, settings.chartsSource)).text()
    if (hash(chartBytes) !== settings.chartsSha256) throw Error("Ligand MD chart manifest hash mismatch")
    const parsed = chartManifest.parse(JSON.parse(chartBytes))
    if (parsed.source_snapshot_sha256 === sha256) {
      for (const chart of parsed.charts) if (hash(await Bun.file(path.join(base, chart.source_path)).text()) !== chart.sha256) throw Error("Ligand MD chart asset hash mismatch")
      chartPackage = parsed
    }
  }
  if (docking.candidates.length !== 3 || new Set(docking.candidates.map(candidate => candidate.compound_id)).size !== 3 || docking.candidates.map(candidate => candidate.rank).sort().join() !== "1,2,3") throw Error("Ligand docking denominator mismatch")
  if (data.scheduler.unresolved.length) throw Error("Ligand MD scheduler observation incomplete")
  const observed = new Date(data.observed_epoch * 1000).toISOString()
  if (!packet.candidates.length || new Set(packet.candidates.map(candidate => settings.candidates[candidate.id])).size !== packet.candidates.length) throw Error("Ligand MD candidate denominator mismatch")
  const rows = packet.candidates.map(candidate => {
    const system = settings.candidates[candidate.id]
    const docked = docking.candidates.find(record => record.compound_id === candidate.id)
    const evidence = data.scientific_outputs[system]
    const job = data.scheduler.items.find(item => item.name === `ligand-md-${system?.split("_")[1]}-20261010-v1`)
    if (!evidence || evidence["status.json"].system_id !== system || !job || job.project !== settings.inspireProject || !docked || docked.system_id !== system) throw Error("Ligand MD candidate identity mismatch")
    const replicas = evidence["status.json"].replicas
    if (new Set(replicas.map(r => r.replica)).size !== replicas.length || replicas.some(r => ![1, 2, 3].includes(r.replica) || r.production_ps !== 10000 || r.frames !== 1000 || !/^[a-f0-9]{64}$/.test(r.analysis_manifest_sha256))) throw Error("Ligand MD replica evidence invalid")
    const production = Object.entries(evidence.replicas).filter(([number, replica]) => {
      if (!/^[123]$/.test(number)) throw Error("Ligand MD replica identity invalid")
      const finished = replica["finished.json"]
      if (!finished) return false
      if (finished.candidate !== system || finished.replica !== Number(number)) throw Error("Ligand MD production identity mismatch")
      if (finished.status !== "REPLICA_PRODUCTION_COMPLETE_REQUIRES_AUDIT") return false
      if (finished.smoke_only || finished.production_ps !== 10000 || finished.production_frames !== 1000) throw Error("Ligand MD production evidence invalid")
      return true
    })
    const verifiedReplicas = replicas.flatMap(replica => {
      const output = evidence.replicas[String(replica.replica)]
      if (!production.some(([number]) => Number(number) === replica.replica)) throw Error("Ligand MD analysis lacks completed production")
      if (!output.analysis_manifest_text) return []
      if (hash(output.analysis_manifest_text) !== replica.analysis_manifest_sha256) throw Error("Ligand MD analysis manifest hash mismatch")
      const analysis = manifest.parse(JSON.parse(output.analysis_manifest_text))
      if (!output.analysis_audit_text || hash(output.analysis_audit_text) !== analysis.files["audit.json"]) throw Error("Ligand MD trajectory audit hash mismatch")
      const audit = manifest.omit({ files: true }).extend({ status: z.literal("FULL_REPLICA_TRAJECTORY_AUDITED") }).parse(JSON.parse(output.analysis_audit_text))
      const finished = output["finished.json"]!
      if (analysis.candidate !== system || analysis.replica !== replica.replica || analysis.input_manifest_sha256 !== finished.manifest_sha256 || analysis.initial_velocity_file_sha256 !== finished.initial_velocity_file_sha256 || audit.candidate !== analysis.candidate || audit.replica !== analysis.replica || audit.input_manifest_sha256 !== analysis.input_manifest_sha256 || audit.initial_velocity_file_sha256 !== analysis.initial_velocity_file_sha256 || output["short-qc.json"]?.status !== "PASS_SHORT_PREP_GEOMETRY" || output["energy-force-check.json"]?.passed !== true) throw Error("Ligand MD analysis identity or QC mismatch")
      if (output.file_verification && (output.file_verification.candidate !== system || output.file_verification.replica !== replica.replica || output.file_verification.input_manifest_sha256 !== analysis.input_manifest_sha256 || output.file_verification.analysis_manifest_sha256 !== replica.analysis_manifest_sha256 || output.file_verification.initial_velocity_file_sha256 !== analysis.initial_velocity_file_sha256)) throw Error("Ligand MD metric verification identity mismatch")
      return [{ replica: replica.replica, metrics: output.file_verification }]
    })
    const verified = verifiedReplicas.length
    const verifiedMetrics = verifiedReplicas.flatMap(replica => replica.metrics ? [replica.metrics] : [])
    if (verifiedMetrics.length && verifiedMetrics.length !== verified) throw Error("Ligand MD metrics are incomplete for verified replicas")
    const terminal = evidence["status.json"].state === "THREE_REPLICAS_AUDITED_ANALYSIS_COMPLETE"
    if (terminal !== evidence["status.json"].analysis_complete || terminal && replicas.length !== 3) throw Error("Ligand MD completion evidence inconsistent")
    const complete = terminal && verified === 3
    const failed = !complete && (evidence["status.json"].state === "NEEDS_REVIEW" || /FAILED|ERROR|CANCELLED|CANCELED|STOPPED|TIMEOUT|TIMED_OUT/.test(job.status))
    const state = complete ? "ready" as const : failed ? "failed" as const : job.status === "RUNNING" ? "running" as const : "pending" as const
    const failure = Object.entries(evidence.replicas).filter(([, replica]) => replica.analysis_log_tail?.includes("AssertionError") && replica.analysis_log_tail.includes("np.allclose") && replica.analysis_log_tail.includes("positions_nm")).map(([number]) => `第 ${number} 重复轨迹坐标一致性审计失败`)
    const missing = [1, 2, 3].filter(number => !evidence.replicas[String(number)])
    const summary = `${system}：${production.length}/3 条 10 ns 生产完成，${verified}/3 条分析清单与身份校验通过${replicas.length > verified ? `（另有 ${replicas.length - verified} 条清单待同步校验）` : ""}；${complete ? "三重复计算与分析完成" : failed ? `流程需复核，停止于重复 ${evidence["status.json"].active_replica ?? "未提供"}${failure.length ? `；${failure.join("；")}` : ""}${missing.length ? `；第 ${missing.join("、")} 重复未启动` : ""}` : `观测时作业 ${job.status}`}。观测时间 ${observed}；计算完成不等于采样收敛或活性验证。`
    const chartRows = chartPackage?.charts.filter(chart => chart.candidate === candidate.id && chart.system === system && chart.replicas.join() === verifiedReplicas.map(row => row.replica).join() && verifiedMetrics.length === verified) ?? []
    return { candidate, docked, count: verified, production: production.length, state, summary, metrics: verifiedMetrics.length ? aggregateMetrics(verifiedMetrics) : [], charts: chartRows }
  })
  const count = rows.reduce((n, r) => n + r.count, 0)
  const production = rows.reduce((n, r) => n + r.production, 0)
  const complete = rows.every(row => row.state === "ready")
  const failed = rows.some(row => row.state === "failed")
  const running = rows.some(row => row.state === "running")
  const notice = `MD ${complete ? "计算与分析完成" : failed ? "部分完成，失败阶段需复核" : running ? "运行中" : "待验收"}：${rows.length} 个候选各计划 3 × 10 ns；${production}/${rows.length * 3} 条生产完成（${production * 10} ns），${count}/${rows.length * 3} 条分析已校验。状态快照：${observed}。三候选 CovDock-LO、独立评价与 Pareto 已完成；轨迹完成不等于采样收敛或活性验证。`
  const stages = original.stages.map(stage => stage.id === "dock" ? { ...stage, state: "done" as const, status: "3 / 3 已完成", count: "3 / 3", title: "CovDock-LO 三候选已完成", detail: `最终对接运行 ${docking.covdock_run} 已完成；MD 分别使用 066 / 060 / 058 的姿势 7 / 6 / 4。`, facts: rows.map(row => `${row.docked.system_id}：评分 ${row.docked.covdock_score.toFixed(3)}，姿势 ${row.docked.pose}`) }
    : stage.id === "review" ? { ...stage, state: "done" as const, status: "评价与排序完成", count: "3 / 3 Gate B", title: "独立评价与 Pareto 已完成", detail: `独立评价 ${docking.evaluation_run} 与 Pareto ${docking.pareto_run} 已完成；计算优先级为 066 → 060 → 058。`, facts: ["三候选均通过独立门控", "均为 Gate B", "不代表实验活性排序"] }
    : stage.id === "md" ? { ...stage, state: complete ? "done" as const : failed ? "blocked" as const : running ? "current" as const : "pending" as const, status: complete ? "计算与分析完成" : failed ? "部分完成 · 需复核" : running ? "MD 运行中（观测快照）" : "MD 待证据验收", count: `${count}/${rows.length * 3} 分析`, title: "Ligand-first · 重复 MD", detail: notice, facts: rows.map(row => row.summary) } : stage)
  const sources = [...packet.sources, { path: settings.dockingSource, sha256: dockingSha256, updated: docking.completed_at }, { path: settings.source, sha256, updated: observed },
    ...(chartPackage && settings.chartsSource && settings.chartsSha256 ? [{ path: settings.chartsSource, sha256: settings.chartsSha256, updated: observed }] : [])]
  const charts = rows.flatMap(row => row.charts.map(chart => ({ id: chart.id, candidate: chart.candidate, kind: chart.kind, title: chart.title, caption: chart.caption,
    asset: { storage: "project" as const, path: chart.project_path, sha256: chart.sha256, mediaType: chart.media_type, name: path.basename(chart.project_path) }, xUnit: chart.x_unit, yUnit: chart.y_unit })))
  const candidates = packet.candidates.map(candidate => {
    const row = rows.find(row => row.candidate.id === candidate.id)!
    const metrics = candidate.metrics.map(metric => metric.id === "covdock_score"
      ? { ...metric, value: row.docked.covdock_score } : metric.id === "local_strain_energy"
        ? { ...metric, value: row.docked.local_strain_energy } : metric)
    const measurements = [
      ...metrics.filter(metric => ["covdock_score", "local_strain_energy"].includes(metric.id)).map(metric => ({
        metricID: metric.id, scope: "candidate" as const, stage: "adduct" as const,
        method: "CovDock-LO bound docking evidence", definition: metric.definition, unit: metric.unit,
        inputs: [{ path: settings.dockingSource, sha256: dockingSha256 }],
        evidence: { path: settings.dockingSource, sha256: dockingSha256 },
      })),
      ...row.metrics.map(metric => ({
        metricID: metric.id, scope: "md" as const, stage: "md" as const,
        method: "Aggregate verified replica means", definition: metric.definition, unit: metric.unit,
        inputs: [{ path: settings.source, sha256 }],
        evidence: { path: settings.source, sha256 },
      })),
    ]
    return { ...candidate, featured: true, badge: "对接已完成 · Gate B", tone: "blue" as const, note: row.summary,
      metrics,
      scientificEvidence: { ...candidate.scientificEvidence, schema: "candidate-scientific-evidence-v1" as const,
        candidateID: candidate.id, smiles: candidate.smiles,
        measurements: [...(candidate.scientificEvidence?.measurements ?? []).filter(item =>
          item.scope !== "md" && !measurements.some(next => next.scope === item.scope && next.metricID === item.metricID)),
          ...measurements] },
      md: { state: row.state, independentReplicas: row.count, summary: row.summary, metrics: row.metrics, chartIds: row.charts.map(chart => chart.id) } }
  })
  const stats = [{ label: "MD 候选", value: String(rows.length), unit: "个", note: "058 / 060 / 066" },
    { label: "生产完成的重复", value: String(production), unit: `/ ${rows.length * 3}`, note: `累计 ${production * 10} ns；生产完成后仍须分析验收` },
    { label: "已校验分析的重复", value: String(count), unit: `/ ${rows.length * 3}`, note: complete ? "全部分析清单与身份校验通过" : "保留已完成重复；其余状态见候选详情" },
    { label: "需复核的候选", value: String(rows.filter(row => row.state === "failed").length), unit: "个", note: observed }]
  const updated = { ...original, current: "md", notice, mdReason: notice, stages, stats, sources, evidenceUpdatedAt: observed,
    candidates: original.candidates.map(candidate => { const row = rows.find(row => row.candidate.id === candidate.row.compound_id)!; return { ...candidate, featured: true, badge: "对接已完成 · Gate B", tone: "blue" as const, note: row.summary,
      row: { ...candidate.row, covdock_score: String(row.docked.covdock_score), local_strain_energy: String(row.docked.local_strain_energy), covdock_pose_index: String(row.docked.pose), covdock_structure_pose: String(row.docked.pose), covdock_gate_detail: row.docked.gate, covdock_gate_reason: "final_covdock_completed_pose_consistency_not_gate_a" } } }) }
  return result.parse({ ...packet, current: "md", observation: "scheduler", notice, stats, stages: stages.map(stage => ({ ...stage, state: stage.state === "done" ? "completed" : stage.state === "current" ? "running" : stage.state })), candidates, charts, sources, evidenceUpdatedAt: observed,
    analysis: { state: complete ? "ready" : failed ? "failed" : "waiting", expectedReplicas: rows.length * 3, observedReplicas: count, message: complete ? "全部重复生产与分析清单已校验；图表和报告按实际归档记录展示。" : failed ? "部分生产或分析阶段失败；保留已验收重复，失败阶段需复核。" : "已完成的生产与已校验分析分别计数；剩余重复等待完成或证据同步。" }, legacy: { kind: "refinement", data: updated } })
}
