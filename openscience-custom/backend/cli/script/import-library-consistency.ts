// Publish an independent reevaluation, retaining the original B2 artifact.
import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'
import { candidateTable } from '../../../frontend/workspace/src/artifacts/candidate-table'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/library-consistency-20261001')
const receipt = await Bun.file(path.join(root, 'reports/library-viewer-receipt.json')).json()
const summary = await Bun.file(path.join(folder, 'evaluation-summary.json')).json()
const sha = (bytes: Uint8Array) => new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
const files = ['REVIEW.md', 'v2-consistency-audit.json', 'evaluation-summary.json', 'library_evaluated_candidates.csv']
const tableBytes = new Uint8Array(await Bun.file(path.join(folder, files[3])).arrayBuffer())
if (sha(tableBytes) !== summary.result_table_sha256 || summary.candidate_count !== 8 || summary.gate_counts.A !== 3 || summary.gate_counts.B1 !== 5) throw Error('Evaluation changed')
if (sha(new Uint8Array(await Bun.file(path.join(folder, files[1])).arrayBuffer())) !== summary.audit_sha256) throw Error('Audit changed')
const table = candidateTable(new TextDecoder().decode(tableBytes))!
if (table.rows.length !== 8) throw Error('Coverage mismatch')
const original = await ArtifactStore.get(receipt.project, receipt.artifact.id)
if (!original?.versions.some(v => v.id === receipt.artifact.current.id && v.sha256 === summary.source_table_sha256)) throw Error('Frozen source not found')
for (const row of table.rows) for (const method of ['af3', 'covdock']) {
  const artifact = await ArtifactStore.get(receipt.project, row[method + '_artifact_id'])
  if (!artifact?.versions.some(v => v.id === row[method + '_artifact_version'] && v.sha256 === row[method + '_artifact_sha256'])) throw Error('Structure reference changed')
}
if (process.argv.includes('--apply')) {
  const target = path.join(folder, 'platform-receipt.json')
  if (await Bun.file(target).exists()) throw Error('Already published; inspect existing receipt')
  const artifacts = []
  for (const filename of files) {
    const content = Bun.file(path.join(folder, filename))
    const artifact = await ArtifactStore.save({ projectID: receipt.project, sessionID: receipt.session,
      sourcePath: path.join(folder, filename), filename, content,
      kind: filename.endsWith('.csv') ? 'table' : filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.csv') ? 'text/csv' : filename.endsWith('.md') ? 'text/markdown' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.csv') ? 'Library-first · 一致性复核 · 3 个 A／5 个 B1' :
        filename.endsWith('.md') ? 'Library-first · 一致性复核说明与证据边界' : 'Library-first · ' + filename })
    artifacts.push(artifact)
  }
  await Bun.write(target, JSON.stringify({ project: receipt.project, session: receipt.session, url: receipt.url, artifacts,
    original_artifact: receipt.artifact.id, original_modified: false, engine_submitted: false }, null, 2))
}
console.log(JSON.stringify({ candidates: table.rows.length, gates: summary.gate_counts, wrote_platform: process.argv.includes('--apply') }))
