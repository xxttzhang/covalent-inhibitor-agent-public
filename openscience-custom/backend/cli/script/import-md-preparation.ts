import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/first-md-20261001/delivery-20261002')
const original = await Bun.file(path.join(root, 'reports/three-route-evaluation-20261001/platform-receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'manifest.json')).json()
for (const [filename, digest] of Object.entries(manifest.files)) {
  const actual = new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest('hex')
  if (actual !== digest) throw Error('MD preparation file changed: ' + filename)
}
const preparation = await Bun.file(path.join(folder, 'preparation.json')).json()
if (preparation.systems.length !== 4 || preparation.trajectory_count !== 0) throw Error('Unexpected preparation coverage')
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of ['MD_PREPARATION.md', 'preparation.csv', 'preparation.json', 'md_prepared_systems.zip', 'manifest.json']) {
    const artifact = await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: filename.endsWith('.csv') ? 'table' : filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.csv') ? 'text/csv' : filename.endsWith('.md') ? 'text/markdown' : filename.endsWith('.zip') ? 'application/zip' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.md') ? '首批 MD · 三候选与阿法替尼对照 · 参数化已核验' :
        filename.endsWith('.csv') ? '首批 MD · 4 套体系准备进度 · 尚无轨迹' : '首批 MD 准备 · ' + filename })
    artifacts.push(artifact)
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project, session: original.session,
    url: original.url, artifacts, prepared_system_count: 4, trajectory_count: 0 }, null, 2))
}
console.log(JSON.stringify({ prepared_systems: 4, wrote_platform: process.argv.includes('--apply') }))
