import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/md-replicas-20261003-v1')
const original = await Bun.file(path.join(root, 'reports/md-validation-20261003/platform-receipt.json')).json()
const plan = await Bun.file(path.join(folder, 'execution-plan.json')).json()
const smoke = await Bun.file(path.join(folder, 'cpu-smoke-verification.json')).json()
const manifest = await Bun.file(path.join(folder, 'handoff-manifest.json')).json()
const archive = await Bun.file(path.join(folder, 'archive.json')).json()
if (plan.status !== 'PREPARED_NOT_SUBMITTED' || plan.runs.length !== 12 || plan.total_initial_production_ns !== 120 ||
  smoke.scientific_replica_results !== 0 || smoke.tests.length !== 5 || manifest.scientific_replicas_started !== 0 ||
  archive.submitted !== false) throw Error('Unexpected replica preparation state')
for (const [filename, digest] of Object.entries(manifest.files)) {
  if (new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest('hex') !== digest)
    throw Error('Frozen replica input changed: ' + filename)
}
if (new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, archive.filename)).arrayBuffer()).digest('hex') !== archive.sha256)
  throw Error('Archive identity changed')
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of ['MD_REPLICA_PLAN.md', 'execution-plan.json', 'evaluation-policy.json', 'CHEMICAL_REVIEW.json',
    'cpu-smoke-verification.json', 'md-replicas-frozen-inputs.tar.gz', 'archive.json', 'handoff-manifest.json']) {
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.md') ? 'text/markdown' : filename.endsWith('.gz') ? 'application/gzip' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.md') ? '重复 MD · 四体系各三次 · 输入已准备／尚未提交' : '重复 MD 准备 · ' + filename }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, prepared_runs: 12, submitted_jobs: 0 }, null, 2))
}
console.log(JSON.stringify({ prepared_runs: 12, submitted_jobs: 0, wrote_platform: process.argv.includes('--apply') }))
