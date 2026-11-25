import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/three-route-evaluation-20261001')
const original = await Bun.file(path.join(root, 'reports/denovo-covdock-20260929/platform-comparison-v1/platform_receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'delivery-manifest.json')).json()
const sha = (bytes: Uint8Array) => new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
for (const [filename, digest] of Object.entries(manifest.source_files)) {
  if (sha(new Uint8Array(await Bun.file(path.join(folder, filename)).arrayBuffer())) !== digest) throw Error('Evaluation file changed: ' + filename)
}
const evaluation = await Bun.file(path.join(folder, 'evaluation.json')).json()
if (evaluation.records.length !== 18 || evaluation.cross_route_rank !== null) throw Error('Evaluation coverage or ranking changed')
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of ['STANDARD.md', 'summary.csv', 'evaluation.json', 'delivery-manifest.json']) {
    const artifact = await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: filename.endsWith('.csv') ? 'table' : filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.csv') ? 'text/csv' : filename.endsWith('.md') ? 'text/markdown' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.md') ? '三路线 · 统一评价标准与当前进展' :
        filename.endsWith('.csv') ? '三路线 · 18 个候选 · 统一证据分级' : '三路线评价 · ' + filename })
    artifacts.push(artifact)
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project, session: original.session,
    url: original.url, artifacts, candidate_count: 18, engine_submitted: false }, null, 2))
}
console.log(JSON.stringify({ candidates: 18, wrote_platform: process.argv.includes('--apply') }))
