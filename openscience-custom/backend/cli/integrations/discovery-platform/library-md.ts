import path from "node:path"
import { stat } from "node:fs/promises"
import { result, type DiscoveryResult } from "@synsci/util/discovery-result"
import { projectPath } from "@synsci/util/discovery-config"
import { mdMetricDefinitions, reportedNumber } from "@synsci/util/metric-semantics"
import type { Binding } from "./binding"
import type { Refinement } from "../../../../frontend/workspace/src/discovery/refinement-model"

type Receipt = { project: string; session: string; artifacts: { id: string; title: string; kind: string; projectID: string; current: { id: string; filename: string; sha256: string; sourcePath: string; mimeType: string; sessionID: string } }[] }
const hash = (data: Uint8Array) => new Bun.CryptoHasher("sha256").update(data).digest("hex")
export async function libraryMD(packet: DiscoveryResult, original: Refinement, base: string, settings: NonNullable<Binding["libraryMD"]>) {
  if (packet.route !== "library-first" || original.route !== packet.route) throw Error("Library MD route mismatch")
  const sources = [...packet.sources]
  const read = async (name: string, expected?: string) => {
    const relative = settings.directory + "/" + projectPath.parse(name)
    const file = path.join(base, relative)
    const bytes = new Uint8Array(await Bun.file(file).arrayBuffer())
    const sha256 = hash(bytes)
    if (!expected || sha256 !== expected) throw Error("Library MD evidence hash mismatch: " + name)
    sources.push({ path: relative, sha256, updated: (await stat(file)).mtime.toISOString() })
    return new TextDecoder().decode(bytes)
  }
  const json = async (name: string) => JSON.parse(await read(name, settings.pins[name]))
  const [verification, manifest, mapping, publication, review, replicas] = await Promise.all([
    json("review-v1/verification.json"), json("analysis-v1/manifest.json"), json("source-manifest.json"),
    json("platform/publication-receipt.json") as Promise<Receipt>, json("review-v1/publication-receipt.json") as Promise<Receipt>, json("review-v1/replica-review-evidence.json"),
  ])
  if (verification.status !== "COMPUTATION_ANALYSIS_DELIVERY_VERIFIED" || verification.automatic_package_manifest_sha256 !== settings.pins["analysis-v1/manifest.json"] || verification.scientific_convergence_established !== false || manifest.replicas !== verification.replicas || manifest.frames !== verification.frames || manifest.systems.length !== packet.candidates.length) throw Error("Library MD completion evidence mismatch")
  for (const receipt of [publication, review]) if (receipt.project !== packet.project || receipt.session !== original.session || receipt.artifacts.some(a => a.projectID !== packet.project || a.current.sessionID !== original.session)) throw Error("Library MD publication scope mismatch")
  const artifacts = [...publication.artifacts, ...review.artifacts]
  // Every published chart/report is bound to its actual immutable bytes, not just a delivery flag.
  for (const artifact of artifacts) {
    const relative = path.relative(path.join(base, settings.directory), artifact.current.sourcePath)
    await read(projectPath.parse(relative), artifact.current.sha256)
  }
  const summaryArtifact = review.artifacts.find(a => a.current.filename === "system-summary.csv")
  if (!summaryArtifact) throw Error("Missing Library MD summary")
  const csv = await read("review-v1/system-summary.csv", summaryArtifact.current.sha256)
  // This versioned numeric summary contains no quoted fields. Reject unexpected formats.
  const [header, ...lines] = csv.trim().split(/\r?\n/).map(line => line.split(","))
  if (csv.includes('"') || new Set(header).size !== header.length || lines.some(line => line.length !== header.length)) throw Error("Invalid Library MD summary CSV")
  const summary = lines.map(line => Object.fromEntries(header.map((key, i) => [key, line[i]])))
  if (!summary || summary.length !== packet.candidates.length || replicas.length !== verification.replicas) throw Error("Library MD denominator mismatch")
  const bindings = mapping.records as { compound_id: string; system_id: string; input_sdf_index: number; sha256: string }[]
  if (new Set(bindings.map(b => b.compound_id)).size !== packet.candidates.length || new Set(bindings.map(b => b.system_id)).size !== manifest.systems.length) throw Error("Library MD duplicate identity")
  const reference = (name: string) => {
    const item = artifacts.find(a => a.current.filename === name)
    if (!item) throw Error("Missing published Library MD file: " + name)
    return { storage: "artifact" as const, id: item.id, version: item.current.id, sha256: item.current.sha256, name, mediaType: item.current.mimeType }
  }
  const charts: DiscoveryResult["charts"] = []
  const candidates = packet.candidates.map(candidate => {
    const source = bindings.find(b => b.compound_id === candidate.id)
    const row = original.candidates.find(c => c.row.compound_id === candidate.id)!.row
    if (!source || String(source.input_sdf_index) !== row.input_sdf_index || source.sha256 !== row.covdock_artifact_sha256 || !manifest.systems.includes(source.system_id)) throw Error("Library MD candidate identity mismatch")
    const runs = replicas.filter((r: { candidate: string }) => r.candidate === source.system_id)
    if (runs.map((r: { replica: number }) => r.replica).sort().join() !== "1,2,3" || runs.some((r: { frames: number; production_ns: number; short_QC: string }) => r.frames !== 1000 || r.production_ns !== 10 || r.short_QC !== "PASS_SHORT_PREP_GEOMETRY")) throw Error("Library MD replica coverage mismatch")
    const record = summary.find(s => s.system_id === source.system_id)
    if (!record || Number(record.replicas) !== runs.length) throw Error("Library MD metric identity mismatch")
    const figures = manifest.charts.filter((c: { system: string }) => c.system === source.system_id)
    if (figures.length !== 7) throw Error("Library MD charts incomplete")
    for (const figure of figures) {
      const asset = reference(figure.file)
      if (asset.sha256 !== figure.sha256 || manifest.files[figure.file] !== figure.sha256) throw Error("Library MD figure hash mismatch")
      charts.push({ id: source.system_id + ":" + figure.kind, candidate: candidate.id, kind: figure.kind, title: figure.title, caption: figure.caption, asset, xUnit: "see figure axes", yUnit: "see figure axes" })
    }
    const metrics = mdMetricDefinitions("library-first", runs.length).map(entry => {
      const mean = reportedNumber(record[entry.key + "_mean"])
      const sd = reportedNumber(record[entry.key + "_replica_SD"])
      return { id: entry.id, label: entry.label, unit: entry.unit,
        value: mean === null ? null : mean * entry.scale, definition: entry.definition,
        ...(mean === null || sd === null ? {} : {
          uncertainty: { kind: "SD" as const, value: sd * entry.scale, independentN: runs.length } }),
      }
    })
    return { ...candidate, note: settings.notes[source.system_id] ?? "MD 已完成；保留全部重复，采样收敛尚未证明。",
      scientificEvidence: { ...candidate.scientificEvidence, schema: "candidate-scientific-evidence-v1" as const,
        candidateID: candidate.id, smiles: candidate.smiles,
        measurements: [...(candidate.scientificEvidence?.measurements ?? []).filter(item => item.scope !== "md"),
          ...metrics.filter(metric => metric.value !== null).map(metric => ({ metricID: metric.id, scope: "md" as const, stage: "md" as const,
            method: "Mean of three audited replica time means", unit: metric.unit, definition: metric.definition,
            inputs: [{ path: settings.directory + "/review-v1/replica-review-evidence.json",
              sha256: settings.pins["review-v1/replica-review-evidence.json"] }],
            evidence: { path: settings.directory + "/review-v1/system-summary.csv", sha256: summaryArtifact.current.sha256 },
          }))] },
      md: { state: "ready" as const, independentReplicas: runs.length, summary: "3 × 10 ns 已验收；计算完成不等于采样收敛或活性验证。", metrics, chartIds: charts.filter(c => c.candidate === candidate.id).map(c => c.id) } }
  })
  const stages = original.stages.map(stage => stage.id === "md" ? { ...stage, label: "重复 MD", state: "done" as const, status: "计算与分析完成", count: `${verification.replicas} / ${verification.replicas} 条`, title: "MD 已完成 · 结果复核", detail: `${candidates.length} 个候选各 3 × 10 ns，共 ${verification.production_ns} ns、${verification.frames.toLocaleString("en-US")} 帧。补环与参数准备已完成，全部轨迹通过审计；重复间差异保留，尚未证明采样收敛。`, facts: ["OpenMM / OPLS_2005", "57 张分析图", "含 RMSF 与直接氢键", "科学复核附录已登记"] } : stage)
  const stats = [{ label: "本轮 MD 候选", value: String(candidates.length), unit: "个", note: "A / B1 原始门控等级保留" }, { label: "独立重复轨迹", value: String(verification.replicas), unit: "/ 24", note: "逐条 QC 与全轨迹审计通过" }, { label: "累计生产段采样", value: String(verification.production_ns), unit: "ns", note: "各候选 3 × 10 ns" }, { label: "自动分析图表", value: String(manifest.charts.length), unit: "张", note: "7 类 × 8 候选 + 汇总图" }]
  const reports = artifacts.filter(a => a.kind !== "figure" || a.current.filename === "batch-summary.png").map(a => ({ title: a.title, asset: reference(a.current.filename) }))
  // Deterministic source order keeps polling from remounting an unchanged plot.
  sources.sort((a, b) => a.path.localeCompare(b.path))
  const updated = { ...original, stages, stats, current: "md", notice: "8 个候选的 MD 与自动分析全部完成；原始 A / B1 为对接阶段的一致性记录，不能当作 MD 排名或活性等级。", mdReason: "已完成 24 条正式轨迹并通过审计。", candidates: original.candidates.map(c => ({ ...c, note: candidates.find(v => v.id === c.row.compound_id)!.note })), sources, evidenceUpdatedAt: sources.map(s => s.updated).sort().at(-1)!, reports: [...original.reports, ...reports.map(r => ({ id: r.asset.id, version: r.asset.version, name: r.asset.name, title: r.title, sha256: r.asset.sha256 }))] }
  return result.parse({ ...packet, current: "md", notice: updated.notice, candidates, charts, stats, stages: stages.map(s => ({ ...s, state: s.state === "done" ? "completed" : s.state === "current" ? "running" : s.state })), reports: [...packet.reports, ...reports], sources, evidenceUpdatedAt: updated.evidenceUpdatedAt, analysis: { state: "ready", expectedReplicas: verification.replicas, observedReplicas: verification.replicas, message: "自动分析及平台交付完成；Codex 复核已登记，非独立人工专家签字。" }, legacy: { kind: "refinement", data: updated } })
}
