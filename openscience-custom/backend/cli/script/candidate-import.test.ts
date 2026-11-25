import { expect, test } from 'bun:test'
import { mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { tmpdir } from '../test/fixture/fixture'
import { Instance } from '../src/project/instance'
import { Session } from '../src/session'
import { ArtifactStore } from '../src/artifact/store'
import { candidateTable } from '../../../frontend/workspace/src/artifacts/candidate-table'
import { Global } from '../src/global'

const workspace = path.resolve(import.meta.dir, '../../../..')
const source = path.join(workspace, 'science/reports/ligand_viewer_20260924/display_manifest.json')
test.skipIf(!await Bun.file(source).exists())('generic manifest imports existing verified structures in dry-run only', async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'candidate-import-'))
  try {
    const existing = await Bun.file(source).json()
    const filename = path.join(folder, 'manifest.json')
    await Bun.write(filename, JSON.stringify({ schema: 'candidate-results-v1', route: 'ligand-first',
      task_id: 'read-only-import-test', assets: existing.assets, candidates: existing.candidates }))
    const child = Bun.spawn([process.execPath, '--no-env-file', path.join(import.meta.dir, 'import-candidate-results.ts'), '--manifest', filename],
      { stdout: 'pipe', stderr: 'pipe' })
    expect(await child.exited).toBe(0)
    const result = JSON.parse(await new Response(child.stdout).text())
    expect(result).toEqual({ validated: true, candidates: 3, assets: 4, documents: 0, wrote_platform: false })
  } finally { await rm(folder, { recursive: true }) }
})

test('unified v2 de-novo manifest validates through the generic dry-run importer', async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'candidate-v2-import-'))
  try {
    const filename = path.join(folder, 'manifest.json')
    const report = path.join(folder, 'selection.md')
    await Bun.write(report, '# Selection\n')
    const bytes = new Uint8Array(await Bun.file(report).arrayBuffer())
    const sha256 = new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
    await Bun.write(filename, JSON.stringify({ schema: 'candidate-results-v2', route: 'de-novo',
      task_id: 'new-denovo-task', assets: {}, documents: { selection_report: {
        path: report, source: report, sha256, size: bytes.length, mime_type: 'text/markdown', title: 'Selection report',
      } }, candidates: [{
        compound_id: 'generated-1', input_sdf_index: '0', standardized_smiles: 'C=CC(=O)N',
        target_id: 'EGFR', site_id: 'A:CYS:797', parent_id: 'not_applicable',
        warhead_class: 'acrylamide', electrophilic_atom: 'C1', nucleophilic_atom: 'A:CYS:797:SG',
        structure_source: 'registered-run', af3_status: 'NOT_RUN', af3_mpae: '',
        covdock_status: 'FAILED', covdock_score: '', covdock_failure_reason: 'no accepted pose',
      }] }))
    const child = Bun.spawn([process.execPath, '--no-env-file', path.join(import.meta.dir, 'import-ligand-viewer.ts'), '--manifest', filename],
      { stdout: 'pipe', stderr: 'pipe' })
    const [status, output, errors] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()])
    if (status !== 0) throw new Error(`Unified import failed: ${errors}\n${output}`)
    expect(status).toBe(0)
    expect(JSON.parse(output)).toEqual({ validated: true, candidates: 1, assets: 0, documents: 1, wrote_platform: false })
  } finally { await rm(folder, { recursive: true }) }
})

test('new task imports into its actual session, retains heterogeneous missing/failure fields, and is idempotent', async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ title: 'Isolated new Ligand result test' })
    const manifest = path.join(tmp.path, 'display_manifest.json')
    await Bun.write(manifest, JSON.stringify({ schema: 'candidate-results-v1', route: 'ligand-first', task_id: 'new-ligand-result-test', assets: {},
      candidates: [{ compound_id: 'new-one', input_sdf_index: '0', standardized_smiles: 'CCO', covdock_gate_detail: 'B1' },
        { compound_id: 'failed-two', input_sdf_index: '1', standardized_smiles: 'CCC', covdock_gate_detail: 'C', covdock_gate_reason: 'hard physical failure', af3_failure_reason: 'no output' }] }))
    for (const _ of [1, 2]) {
      const child = Bun.spawn([process.execPath, '--no-env-file', path.join(import.meta.dir, 'import-ligand-viewer.ts'),
        '--manifest', manifest, '--directory', tmp.path, '--session', session.id, '--apply'], { stdout: 'pipe', stderr: 'pipe',
          env: { ...process.env, OPENSCIENCE_DATA_DIR: Global.Path.data, OPENSCIENCE_CONFIG_DIR: Global.Path.config, OPENSCIENCE_PORT: '4098' } })
      const [status, output, errors] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()])
      if (status !== 0) throw new Error(`Isolated import failed: ${errors}\n${output}`)
      expect(errors).not.toContain('error:')
      expect(status).toBe(0)
      expect(JSON.parse(output).session).toBe(session.id)
    }
    const artifacts = await ArtifactStore.list(Instance.project.id, 'active')
    const tables = artifacts.filter((item) => item.current.filename === 'ligand_first_viewer_candidates.csv')
    expect(tables).toHaveLength(1)
    expect(tables[0].versionCount).toBe(1)
    const receipt = await Bun.file(path.join(tmp.path, 'platform_receipt.json')).json() as {
      result_artifact_id?: string
      url?: string
    }
    expect(receipt.result_artifact_id).toBe(tables[0].id)
    expect(receipt.url).toBe(`http://127.0.0.1:4098/${Instance.project.id}/session/${session.id}?result=${tables[0].id}`)
    const file = await ArtifactStore.read(Instance.project.id, tables[0].id, tables[0].current.id)
    const table = candidateTable(await file!.content.text())!
    expect(table.rows).toHaveLength(2)
    expect(table.rows[1].covdock_gate_reason).toBe('hard physical failure')
    expect(table.rows[1].af3_failure_reason).toBe('no output')
    expect(table.rows.every((row) => row.task_id === 'new-ligand-result-test' && !row.covdock_artifact_id)).toBe(true)
  } })
}, 15000)

test('v2 report documents are saved in the same session and linked from every candidate row', async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({ title: 'Unified Ligand report linkage' })
    const report = path.join(tmp.path, 'stage6_selection_report.md')
    await Bun.write(report, '# Pareto selection report\n')
    const bytes = new Uint8Array(await Bun.file(report).arrayBuffer())
    const sha256 = new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
    const manifest = path.join(tmp.path, 'display_manifest.json')
    await Bun.write(manifest, JSON.stringify({ schema: 'candidate-results-v2', route: 'ligand-first',
      task_id: 'linked-ligand-report', assets: {}, documents: { selection_report: {
        path: report, source: report, sha256, size: bytes.length, mime_type: 'text/markdown', title: 'Pareto selection report',
      } }, candidates: [{
        compound_id: 'candidate-1', input_sdf_index: '0', standardized_smiles: 'C=CC(=O)N',
        target_id: 'EGFR', site_id: 'A:CYS:797', parent_id: 'erlotinib', warhead_class: 'acrylamide',
        electrophilic_atom: 'C1', nucleophilic_atom: 'A:CYS:797:SG', structure_source: 'none',
        af3_status: 'NOT_RUN', af3_mpae: '', covdock_status: 'FAILED', covdock_score: '',
        covdock_failure_reason: 'no accepted pose',
      }] }))
    const child = Bun.spawn([process.execPath, '--no-env-file', path.join(import.meta.dir, 'import-ligand-viewer.ts'),
      '--manifest', manifest, '--directory', tmp.path, '--session', session.id, '--apply'], { stdout: 'pipe', stderr: 'pipe',
        env: { ...process.env, OPENSCIENCE_DATA_DIR: Global.Path.data, OPENSCIENCE_CONFIG_DIR: Global.Path.config } })
    const [status, output, errors] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()])
    if (status !== 0) throw new Error(`Report import failed: ${errors}\n${output}`)
    const artifacts = await ArtifactStore.list(Instance.project.id, 'active')
    const reportArtifact = artifacts.find((item) => item.current.filename === 'stage6_selection_report.md')
    if (!reportArtifact) throw new Error('Selection report artifact was not saved')
    expect(reportArtifact.current.sha256).toBe(sha256)
    expect(reportArtifact.current.sessionID).toBe(session.id)
    const tableArtifact = artifacts.find((item) => item.current.filename === 'ligand_first_viewer_candidates.csv')!
    const file = await ArtifactStore.read(Instance.project.id, tableArtifact.id, tableArtifact.current.id)
    const table = candidateTable(await file!.content.text())!
    expect(table.rows[0].selection_report_artifact_id).toBe(reportArtifact.id)
    expect(table.rows[0].selection_report_artifact_version).toBe(reportArtifact.current.id)
    expect(table.rows[0].selection_report_artifact_sha256).toBe(sha256)
  } })
}, 15000)
