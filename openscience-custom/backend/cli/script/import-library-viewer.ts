// Read existing delivery receipts and build a display-only joined table.
// No engine invocation and no update to frozen results or execution state.
import { Database } from "bun:sqlite"
import path from "node:path"
import { ArtifactStore } from "../src/artifact/store"
import { candidateTable } from "../../../frontend/workspace/src/artifacts/candidate-table"
import { maestroGeometry, maestroOffsets } from "../../../frontend/workspace/src/artifacts/maestro-preview"
import { gzipPreview } from "../../../frontend/workspace/src/artifacts/gzip-preview"

const root = path.resolve(import.meta.dir, "../../../..")
const argument = process.argv.indexOf('--workflow')
if (argument < 0 && !process.argv.includes('--legacy')) throw new Error('Provide --workflow for a new task; historical import requires explicit --legacy')
const workflow = argument >= 0 ? process.argv[argument + 1] : "refineflow-76378344dae197d0022d4549"
if (!/^refineflow-[a-f0-9]{24}$/.test(workflow ?? '')) throw new Error('Explicit valid workflow ID required')
const db = new Database(path.join(root, ".cache/inspire-executor/tasks.sqlite"), { readonly: true })
const inventory = db.query("SELECT name,sha256,receipt FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL").all(workflow) as { name: string; sha256: string; receipt: string }[]
const snapshot = () => JSON.stringify(db.query("SELECT id,submitted,handle FROM refinement_runs ORDER BY id").all())
const before = snapshot()
type Receipt = { name: string; artifact_id: string; version_id: string; project_id: string; sha256: string; bytes: number; local_path: string }
const entries = inventory.map((entry) => ({ ...entry, ref: JSON.parse(entry.receipt) as Receipt }))
const one = (name: string) => { const matches = entries.filter((entry) => entry.name === name); if (matches.length !== 1) throw new Error(`Ambiguous source: ${name}`); return matches[0] }
const source = one("covdock/stage5_gated_candidates.csv")
const scores = one("af3/af3_all_scores.csv")
const covdock = one("covdock/covdock_lo/library_first_core_subset_covdock_lo-out.maegz")
const verified = new Map<string, Uint8Array>()
async function verify(entry: typeof source) {
  if (verified.has(entry.name)) return verified.get(entry.name)!
  const bytes = new Uint8Array(await Bun.file(entry.ref.local_path).arrayBuffer())
  const sha = new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
  if (sha !== entry.sha256 || sha !== entry.ref.sha256 || bytes.length !== entry.ref.bytes) throw new Error("Source receipt integrity failure")
  const artifact = await ArtifactStore.get(entry.ref.project_id, entry.ref.artifact_id)
  const version = artifact?.versions.find((version) => version.id === entry.ref.version_id)
  if (!version || version.sha256 !== sha || version.size !== bytes.length || artifact?.state !== "active") throw new Error("Stored artifact version mismatch")
  verified.set(entry.name, bytes)
  return bytes
}
const table = candidateTable(new TextDecoder().decode(await verify(source)))!
const af3 = candidateTable(new TextDecoder().decode(await verify(scores)))!
if (!table || !af3 || !table.rows.length || table.rows.length > 100) throw new Error("Candidate table outside bounded 1..100 range")
const complex = new TextDecoder().decode(await gzipPreview(new Uint8Array(await verify(covdock))))
const offsets = maestroOffsets(complex)
const rows: Record<string, string>[] = []
for (const original of table.rows) {
  const row = { ...original, route: 'library-first', task_id: workflow, result_scope: argument >= 0 ? 'candidate-results-v1' : "library_first_refinement_display", source_table_sha256: source.sha256 } as Record<string, string>
  const links: [string, typeof source][] = []
  if (/^\d+$/.test(row.covdock_pose_index ?? '')) {
    const pose = Number(row.covdock_pose_index)
    const geometry = maestroGeometry(complex, pose, offsets)
    if (geometry.inputIndex !== row.input_sdf_index || geometry.compound !== row.compound_id) throw new Error("CovDock identity mismatch")
    row.covdock_structure_pose = String(pose)
    links.push(['covdock', covdock])
  } else if (argument < 0) throw new Error('Historical CovDock pose missing')
  const matches = af3.rows.filter((r) => r.compound_id === row.compound_id && r.input_sdf_index === row.input_sdf_index)
  if (matches.length > 1 || (argument < 0 && (matches.length !== 1 || matches[0].status !== 'SUCCESS'))) throw new Error('AF3 identity ambiguous or unsuccessful')
  if (matches[0]?.status === 'SUCCESS') {
    const model = matches[0]
    const relative = model.model_cif.slice(model.model_cif.indexOf('/af3/predictions/') + 1)
    if (!relative.startsWith('af3/predictions/')) throw new Error('AF3 source outside recorded prediction path')
    const structure = one(relative)
    await verify(structure)
    row.af3_mpae = model.mpae
    links.push(['af3', structure])
  } else row.af3_mpae = ''
  row.af3_score_source_sha256 = scores.sha256
  for (const [method, entry] of links) {
    row[`${method}_artifact_id`] = entry.ref.artifact_id
    row[`${method}_artifact_version`] = entry.ref.version_id
    row[`${method}_artifact_sha256`] = entry.sha256
  }
  rows.push(row)
}
const session = (await ArtifactStore.get(source.ref.project_id, source.ref.artifact_id))!.versions.find((v) => v.id === source.ref.version_id)!.sessionID
if (process.argv.includes("--apply")) {
  const filename = path.join(root, argument >= 0 ? `reports/library_first_viewer_${workflow}.csv` : "reports/library_first_viewer_candidates_20260924.csv")
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))]
  const quote = (text: string) => `"${String(text ?? "").replaceAll('"', '""')}"`
  const csv = [keys.map(quote).join(","), ...rows.map((row) => keys.map((key) => quote(row[key])).join(","))].join("\n") + "\n"
  await Bun.write(filename, csv)
  const sha = new Bun.CryptoHasher("sha256").update(csv).digest("hex")
  const existing = (await ArtifactStore.list(source.ref.project_id, "active")).find((a) => a.current.sourcePath === filename && a.current.sha256 === sha)
  const artifact = existing ?? await ArtifactStore.save({ projectID: source.ref.project_id, sessionID: session, sourcePath: filename,
    filename: path.basename(filename), kind: "table", content: new Blob([csv]), mimeType: "text/csv", captureQuality: "declared",
    title: `Library-first · ${rows.length} 个候选 · 结构与结合口袋总览` })
  await Bun.write(path.join(root, argument >= 0 ? `reports/library-viewer-${workflow}-receipt.json` : "reports/library-viewer-receipt.json"), JSON.stringify({ project: source.ref.project_id, session,
    url: `http://127.0.0.1:4096/${source.ref.project_id}/session/${session}`, artifact, source: source.ref,
    workflow, count: rows.length, new_calculation: false, source_hashes: entries.filter((e) => verified.has(e.name)).map((e) => ({ name: e.name, sha256: e.sha256 })) }, null, 2))
}
if (snapshot() !== before) throw new Error("Scientific execution state changed")
console.log(JSON.stringify({ candidates: rows.length, covdock_poses: rows.map((r) => r.covdock_structure_pose), af3_linked: rows.filter((r) => r.af3_artifact_id).length,
  gates: rows.map((r) => r.covdock_gate_detail), wrote_platform: process.argv.includes("--apply"), scientific_submissions_unchanged: true }))
db.close()
