import type { Binding } from "../discovery-platform/binding"
import path from "node:path"
import { mkdir, rename, stat } from "node:fs/promises"
import { candidateTable } from "../../../../frontend/workspace/src/artifacts/candidate-table"

const root = path.resolve(import.meta.dir, "../../../../..")
const hash = (bytes: string | Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
type Source = { path: string; sha256: string; updated: string }
type Metric = { mean: number; sd_between_replicas: number; replica_estimates: number[] }
type System = { sampling_assessment: string; replica_estimates: Record<string, Metric>; equilibrium_proven: boolean }
type Replica = { candidate: string; replica: number }
type Receipt = { id: string; title: string; current: { id: string; filename: string; sha256: string } }

/** Read observed files only. This never polls/submits compute or rewrites scientific records. */
export async function snapshot(base: string, binding: Binding) {
  const study = binding.study
  const values = binding.values.denovo
  const sources: Source[] = []
  const read = async (name: string) => {
    const filename = path.join(base, name)
    const [text, info] = await Promise.all([Bun.file(filename).text(), stat(filename)])
    sources.push({ path: name, sha256: hash(text), updated: info.mtime.toISOString() })
    return text
  }
  const generation = JSON.parse(await read(values.generationPath))
  if (generation.id !== study.task) throw Error("Generation identity mismatch")
  const review = values.v3
  const comparison = JSON.parse(await read(review + "comparison.json"))
  const auditText = await read(review + "structural_audit.json")
  if (hash(auditText) !== comparison.sources["structural_audit.json"]) throw Error("CovDock audit hash mismatch")
  const audit = JSON.parse(auditText) as { complete_input_coverage: boolean; records: { id: string; poses: { failures: string[] }[] }[] }
  const poses = audit.records.filter(record => record.id.startsWith(values.v4)).flatMap(record => record.poses)
  const table = candidateTable(await read(review + "platform-comparison-v1/denovo_comparison.csv"))
  if (!table || table.rows.length !== 7 || new Set(table.rows.map(row => row.compound_id)).size !== 7 ||
    table.rows.some(row => row.task_id !== study.task || row.route !== "de-novo")) throw Error("Candidate identity mismatch")
  const final = values.v5
  const manifest = JSON.parse(await read(final + "manifest.json"))
  const summaryText = await read(final + "sampling-summary.json")
  if (hash(summaryText) !== manifest.files["sampling-summary.json"]) throw Error("MD summary hash mismatch")
  const summary = JSON.parse(summaryText) as { status: string; systems: Record<string, System>; replicas: Replica[] }
  const scientific = JSON.parse(await read(final + "scientific-review.json"))
  if (scientific.source_manifest_sha256 !== sources.find(s => s.path === final + "manifest.json")?.sha256 ||
    !scientific.scientific_review_completed || summary.status !== "REPEATED_MD_SAMPLING_ASSESSED" ||
    summary.replicas.length !== 12) throw Error("MD review is incomplete")
  for (const id of [values.v6, values.v7, values.v8, values.v9]) {
    if (summary.replicas.filter(r => r.candidate === id).map(r => r.replica).sort().join() !== "1,2,3")
      throw Error("MD replica coverage mismatch")
  }
  const mdReceipt = JSON.parse(await read(final + "platform-receipt.json"))
  const dockReceipt = JSON.parse(await read(review + "platform-comparison-v1/platform_receipt.json"))
  const receipts: Receipt[] = [...mdReceipt.artifacts, ...Object.values(dockReceipt.artifacts)] as Receipt[]
  const reports = ["REPEATED_MD_REPORT_PLATFORM.md", "REPLICA_STRUCTURAL_TRACES.png", "REPLICA_PHYSICAL_TRACES.png", "REPLICA_ESTIMATES.csv", "FINAL_REVIEW.md", "md_shortlist_structures.zip"]
    .map(name => receipts.find(item => item.current.filename === name)).filter((r): r is Receipt => !!r)
    .map(item => ({ id: item.id, version: item.current.id, name: item.current.filename, title: item.title, sha256: item.current.sha256 }))
  const amberRoot = values.v10
  const amberSource = JSON.parse(await read(amberRoot + "source-models-v2/manifest.json"))
  const amberFiles = await Array.fromAsync(new Bun.Glob("**/result.json").scan({ cwd: path.join(base, amberRoot), onlyFiles: true }))
  const probes = []
  for (const name of amberFiles.sort()) {
    const result = JSON.parse(await read(amberRoot + name))
    if (result.phase !== "capped_adduct_probe" || !summary.systems[result.system]) continue
    probes.push({ system: result.system as string, status: result.status as string,
      updated: new Date((result.finished_epoch ?? result.started_epoch) * 1000).toISOString(),
      scope: "封端加合物参数探针", path: amberRoot + name })
  }
  const candidates = table.rows.map(row => {
    const system = summary.systems[row.compound_id]
    const chemistry = comparison.records.find((item: { id: string }) => item.id === row.compound_id)?.chemistry
    const tableSource = sources.find(source => source.path === review + "platform-comparison-v1/denovo_comparison.csv")!
    const summarySource = sources.find(source => source.path === final + "sampling-summary.json")!
    const measurements = [
      ...[["covdock_score", "vendor score"], ["local_strain_energy", "kcal/mol"]]
        .filter(([id]) => row[id]?.trim() && Number.isFinite(Number(row[id])))
        .map(([id, unit]) => ({ metricID: id, scope: "candidate" as const, stage: "adduct" as const,
          method: "Registered CovDock comparison table", unit, definition: "原始工具输出；不跨方法直接比较",
          inputs: [{ path: tableSource.path, sha256: tableSource.sha256 }],
          evidence: { path: tableSource.path, sha256: tableSource.sha256 } })),
      ...(system ? [{ metricID: "ring_rmsd", scope: "md" as const, stage: "md" as const,
        method: "Registered repeated MD sampling summary", unit: "Å", definition: "重复轨迹环系 RMSD 平均值",
        inputs: [{ path: summarySource.path, sha256: summarySource.sha256 }],
        evidence: { path: summarySource.path, sha256: summarySource.sha256 } }] : []),
    ]
    return { row, molecularWeight: chemistry?.molecular_weight_Da ?? null, logP: chemistry?.cLogP ?? null,
      scientificEvidence: { schema: "candidate-scientific-evidence-v1" as const,
        candidateID: row.compound_id, smiles: row.standardized_smiles, measurements },
      md: system ? { assessment: system.sampling_assessment,
        rmsd: system.replica_estimates.largest_ring_system_RMSD_A,
        contacts: system.replica_estimates[values.contactsMetric],
        replicas: summary.replicas.filter(r => r.candidate === row.compound_id).length } : null }
  })
  return { schema: "discovery-workbench-v1", route: "de-novo", ...study, collectedAt: new Date().toISOString(),
    evidenceUpdatedAt: sources.map(s => s.updated).sort().at(-1),
    observation: "local-evidence", refreshSeconds: 15,
    generation: { state: generation.state, updated: new Date(generation.updated * 1000).toISOString(),
      slots: generation.summary?.requested_slots ?? generation.requested_slots,
      passed: generation.summary?.raw.pass_count, relaxedPassed: generation.summary?.relaxed.pass_count },
    docking: { completed: comparison.complete_input_coverage === true && audit.complete_input_coverage, candidates: candidates.length,
      poses: poses.length, passed: poses.filter(pose => pose.failures.length === 0).length, controlRmsd: 1.29 },
    md: { replicas: summary.replicas.length, totalNs: 120, frames: 12000, engine: "OpenMM", forcefield: "OPLS_2005 / TIP3P",
      systems: 4, assessment: "SAMPLING_INSUFFICIENT", control: summary.systems[values.controlId] },
    amber: { sourceSystems: amberSource.systems.length, productionStarted: false, forcefield: "ff19SB / GAFF2", probes },
    candidates, reports, sources }
}

