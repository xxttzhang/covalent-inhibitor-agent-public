// Publish the completed review to its original generation session. No compute.
import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'
import { Instance } from '../src/project/instance'
import { Session } from '../src/session'
import { gzipPreview } from '../../../frontend/workspace/src/artifacts/gzip-preview'
import { denovoGeometry } from '../../../frontend/workspace/src/artifacts/denovo-structure'

const root = path.resolve(import.meta.dir, '../../../..')
const study = path.join(root, 'reports/denovo-covdock-20260929')
const output = path.join(study, 'platform-comparison-v1')
const task = 'denovo-950cd19cedff09505dbf3eac'
const session = 'ses_f14d8c61dffePbcbRvvucbLg1I'
const project = 'prj_dbfd2a34da2e4c78977238fcf59d39ae'
const directory = path.join(root, 'openscience-custom')
const bond = (await Bun.file(path.join(directory, '.discovery/project.json')).json()).routes['de-novo'].bond
const hash = (bytes: Uint8Array) => new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
const read = async (name: string) => new Uint8Array(await Bun.file(name).arrayBuffer())
const check = (ok: unknown, message: string) => { if (!ok) throw new Error(message) }
const completion = await Bun.file(path.join(study, 'completion_audit.json')).json()
for (const [name, digest] of Object.entries(completion.artifacts_sha256)) check(hash(await read(path.join(study, name))) === digest, `Completion artifact changed: ${name}`)
const comparison = await Bun.file(path.join(study, 'comparison.json')).json()
const audit = await Bun.file(path.join(study, 'structural_audit.json')).json()
const selection = await Bun.file(path.join(study, 'md_selection.json')).json()
const gate = await Bun.file(path.join(study, 'control_gate.json')).json()
check(hash(await read(path.join(study, 'receptor_prepared.mae'))) === gate.provenance_files['receptor_prepared.mae'], 'Prepared receptor changed')
const pockets = await Bun.file(path.join(directory, 'denovo-results', task, 'pocket_views/pocket_manifest.json')).json()
check(comparison.complete_input_coverage && audit.complete_input_coverage && !selection.md_started && pockets.task_id === task, 'Incomplete or changed review')
for (const [name, digest] of Object.entries(comparison.sources)) check(hash(await read(path.join(study, name))) === digest, `Comparison source changed: ${name}`)
const records = comparison.records.filter((r: { id: string }) => r.id.startsWith('denovo_')) as { id: string; pose_index_0based: number; eligible: boolean; affinity_score: number; local_strain_kcal_mol: number; solvated_global_strain_kcal_mol: number | null; generated_scaffold_rmsd_A: number; hydrogen_bond_residues: string[]; chemistry: { source_smiles: string } }[]
check(records.map(r => r.id).sort().join(',') === 'denovo_000,denovo_001,denovo_002,denovo_004,denovo_005,denovo_006,denovo_007', 'Candidate coverage changed')
const sources = new Map<string, Uint8Array>()
for (const r of records) {
  const file = path.join(study, r.id, `${r.id}-out.maegz`)
  const bytes = await read(file)
  const geometry = denovoGeometry(new TextDecoder().decode(await gzipPreview(bytes)), r.pose_index_0based, r.id, bond)
  check(r.eligible && geometry.atoms.length > 5000, 'Best pose not eligible or protein incomplete')
  sources.set(r.id, bytes)
}
await Instance.provide({ directory, async fn() {
  check(Instance.project.id === project, 'Unexpected project')
  check((await Session.get(session)).projectID === project, 'Session/project mismatch')
  for (const r of records) {
    const p = pockets.candidates.find((p: { slot: number }) => p.slot === Number(r.id.slice(-3)))
    check(p && hash(await read(p.path)) === p.sha256 && hash(await read(p.source_sdf)) === p.source_sha256, 'Generated structure source mismatch')
    const artifact = await ArtifactStore.get(project, p.artifact_id)
    const version = artifact?.versions.find(v => v.id === p.artifact_version)
    check(artifact?.state === 'active' && version?.sha256 === p.sha256 && version?.sessionID === session, 'Generated artifact reference mismatch')
  }
  if (!process.argv.includes('--apply')) { console.log(JSON.stringify({ validated: true, candidates: records.length, wrote_platform: false })); return }
  const existing = await ArtifactStore.list(project, 'active')
  const saved: Record<string, ArtifactStore.Artifact> = {}
  async function save(file: string, kind: string, title: string, mime: string) {
    const bytes = await read(file)
    const digest = hash(bytes)
    const artifact = existing.find(a => a.current.sourcePath === file && a.current.sha256 === digest && a.current.sessionID === session)
      ?? await ArtifactStore.save({ projectID: project, sessionID: session, sourcePath: file, filename: path.basename(file), kind, title, mimeType: mime, content: new Blob([bytes]), captureQuality: 'declared' })
    saved[path.basename(file)] = artifact
    await Bun.write(path.join(output, 'platform_receipt.json'), JSON.stringify({ project, session, task_id: task, url: `http://127.0.0.1:4096/${project}/session/${session}`, artifacts: saved, new_calculation: false, md_started: false }, null, 2))
    return artifact
  }
  const rows: Record<string, string>[] = []
  for (const r of records) {
    const artifact = await save(path.join(study, r.id, `${r.id}-out.maegz`), 'protein-structure', `De novo · ${r.id.slice(-3)} · CovDock 原始姿势集`, 'application/gzip')
    const p = pockets.candidates.find((p: { slot: number }) => p.slot === Number(r.id.slice(-3)))
    const chosen = selection.selected.find((x: { id: string }) => x.id === r.id)
    rows.push({ route: 'de-novo', result_scope: 'denovo-covdock-comparison-v1', task_id: task,
      compound_id: r.id, input_sdf_index: String(p.slot), standardized_smiles: r.chemistry.source_smiles,
      covdock_score: String(r.affinity_score), local_strain_energy: String(r.local_strain_kcal_mol),
      solvated_global_strain_kcal_mol: r.solvated_global_strain_kcal_mol === null ? '' : String(r.solvated_global_strain_kcal_mol),
      generated_scaffold_rmsd_A: String(r.generated_scaffold_rmsd_A), hydrogen_bond_residues: r.hydrogen_bond_residues.join(', '),
      physical_validity: 'PASS', covdock_structure_pose: String(r.pose_index_0based),
      covdock_artifact_id: artifact.id, covdock_artifact_version: artifact.current.id, covdock_artifact_sha256: artifact.current.sha256,
      covdock_target_context: JSON.stringify({ task_id: task, plan_digest: selection.manifest_sha256, artifact_sha256: artifact.current.sha256, target: 'EGFR', site: 'A:CYS:797:SG', parameters: { target: 'EGFR', receptor: 'receptor_prepared.mae', receptor_sha256: gate.provenance_files['receptor_prepared.mae'], chain: 'A', residue: 'CYS', residue_number: 797, insertion_code: '', numbering: 'structure', reaction_type: 'Michael Addition' } }),
      generated_artifact_id: p.artifact_id, generated_artifact_version: p.artifact_version, generated_artifact_sha256: p.sha256,
      md_priority: chosen ? String(chosen.priority) : '', md_tier: chosen?.tier ?? '', md_reason: chosen?.reason ?? selection.not_selected[r.id], md_limitation: chosen?.limitation ?? '',
      audit_sha256: comparison.sources['structural_audit.json'], selection_sha256: comparison.sources['md_selection.json'] })
  }
  const columns = Object.keys(rows[0])
  const quote = (v: string) => `"${v.replaceAll('"', '""')}"`
  const csv = [columns.map(quote).join(','), ...rows.map(r => columns.map(k => quote(r[k])).join(','))].join('\n') + '\n'
  const table = path.join(output, 'denovo_comparison.csv')
  await Bun.write(table, csv)
  await save(table, 'table', 'De novo · 7 个候选 · 生成／CovDock 比较与 MD 入选', 'text/csv')
  await save(path.join(study, 'FINAL_REVIEW.md'), 'document', 'De novo · CovDock 复核与 MD 入选报告', 'text/markdown')
  await save(path.join(study, 'md_shortlist_structures.zip'), 'file', 'De novo · 004／001／007 完整共价复合物（MD 准备）', 'application/zip')
  for (const name of ['comparison.json', 'md_selection.json', 'structural_audit.json', 'charge_conservation_audit.json']) await save(path.join(study, name), 'file', `De novo · ${name}`, 'application/json')
  console.log(JSON.stringify({ published: true, candidates: rows.length, artifacts: Object.keys(saved).length, output, new_calculation: false }))
} })
await Instance.disposeAll()
