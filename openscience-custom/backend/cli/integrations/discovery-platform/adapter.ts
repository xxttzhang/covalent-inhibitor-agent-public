import { result, type DiscoveryResult } from "@synsci/util/discovery-result"
import { renderTemplate, type DiscoveryProject } from "@synsci/util/discovery-config"
import { workbench, stages, recommendation } from "../../../../frontend/workspace/src/discovery/model"
import { refinement } from "../../../../frontend/workspace/src/discovery/refinement-model"
import { candidateStructures } from "../../../../frontend/workspace/src/artifacts/candidate-structure"

type Analysis = { phase: DiscoveryResult["analysis"]["state"]; count: number; expected: number; error: string; project: string;
 charts: { system: string; kind: string; title: string; caption: string; path: string; sha256: string }[] }
export function adapt(input: unknown, config: DiscoveryProject, route: keyof DiscoveryProject["routes"], analysis?: Analysis) {
  if (!config.target) throw Error("Target must be configured")
  const data = route === "de-novo" ? workbench.parse(input) : refinement.parse(input)
  if (data.project !== config.project || data.route !== route || analysis && analysis.project !== config.project) throw Error("Adapter project mismatch")
  const presentation = config.routes[route]
  const denovo = data.schema === "discovery-workbench-v1" ? data : undefined
  const refined = data.schema === "refinement-workbench-v1" ? data : undefined
  const charts = (analysis?.phase === "ready" ? analysis.charts : []).filter(chart => data.candidates.some(c => c.row.compound_id === chart.system)).map(chart => ({
    id: chart.system + ":" + chart.kind + ":" + chart.path.split(".").at(-1), candidate: chart.system, kind: chart.kind, title: chart.title, caption: chart.caption,
    xUnit: chart.kind === "hbonds" ? "%" : chart.kind === "contacts" ? "ns / residue index" : "ns",
    yUnit: ({ rmsd: "Å", contacts: "% / replica index (color: %)", hbonds: "atom pair", sampling: "Å / %", physical: "K / g·mL⁻¹ / Å" } as Record<string, string>)[chart.kind] ?? "unspecified", asset: { storage: "project", path: chart.path, sha256: chart.sha256, name: chart.path.split("/").at(-1)!, mediaType: chart.path.endsWith("svg") ? "image/svg+xml" : "image/png" },
  }))
  return result.parse({ schema: "discovery-result-v1", project: config.project, target: config.target.id, route,
    run: denovo?.task ?? data.session, collectedAt: data.collectedAt, evidenceUpdatedAt: data.evidenceUpdatedAt, observation: "local-evidence",
    current: refined?.current ?? presentation.defaultStage, notice: refined?.notice ?? "计算候选 · 尚无实验活性结论",
    stages: (denovo ? stages(denovo, presentation) : refined!.stages).map(step => ({ ...step, state: ({ done: "completed", current: "running", pending: "pending", blocked: "blocked" } as Record<string, string>)[step.state] })),
    stats: denovo ? renderTemplate(presentation.stats, { ...denovo, featuredCount: presentation.featured.length }) : refined!.stats,
    candidates: data.candidates.map(candidate => {
      const row = candidate.row
      const md = "md" in candidate ? candidate.md : undefined
      const review = recommendation(row.compound_id, presentation)
      return { id: row.compound_id, label: "label" in candidate ? candidate.label : row.compound_id.split("_").at(-1), smiles: row.standardized_smiles,
        featured: "featured" in candidate ? candidate.featured : presentation.featured.includes(row.compound_id),
        badge: "badge" in candidate ? candidate.badge : review.label, tone: "tone" in candidate ? candidate.tone : review.tone,
        note: "note" in candidate ? candidate.note : review.detail,
        scientificEvidence: "scientificEvidence" in candidate ? candidate.scientificEvidence : undefined,
        metrics: [["covdock_score", "CovDock 评分", "vendor score"], ["local_strain_energy", "局部应变", "kcal/mol"], ["af3_mpae", "AF3 mPAE", "Å"]].map(([id, label, unit]) => ({ id, label, unit, value: row[id]?.trim() && Number.isFinite(Number(row[id])) ? Number(row[id]) : null, definition: "原始工具输出；不跨方法直接比较" })),
        structures: candidateStructures(row).map(ref => ({ method: ref.method, format: ref.method === "af3" ? "cif" : ref.method === "generated" ? "pdb" : "maegz",
          asset: { storage: "artifact", id: ref.id, version: ref.version, sha256: ref.sha256, name: ref.method, mediaType: "chemical/x-structure" }, pose: ref.pose,
          identity: { compound_id: row.compound_id, input_sdf_index: row.input_sdf_index ?? "", task_id: row.task_id ?? "" } })),
        md: { state: md ? "ready" : "pending", summary: md?.assessment ?? refined?.mdReason ?? "本轮未开展 MD", independentReplicas: md?.replicas ?? 0,
          metrics: md ? [{ id: "ring_rmsd", label: "环系 RMSD", value: md.rmsd.mean, unit: "Å", definition: "重复轨迹环系 RMSD 平均值", uncertainty: { kind: "SD", value: md.rmsd.sd_between_replicas, independentN: md.replicas } }] : [],
          chartIds: charts.filter(c => c.candidate === row.compound_id).map(c => c.id) },
      }
    }), analysis: { state: analysis?.phase ?? "waiting", expectedReplicas: analysis?.expected ?? 0, observedReplicas: analysis?.count ?? 0, message: analysis?.error ?? "等待正式轨迹登记" }, charts,
    reports: data.reports.map(report => ({ title: report.title, asset: { storage: "artifact", id: report.id, version: report.version, sha256: report.sha256, name: report.name, mediaType: "application/octet-stream" } })),
    sources: data.sources, legacy: { kind: denovo ? "denovo" : "refinement", data },
  })
}
