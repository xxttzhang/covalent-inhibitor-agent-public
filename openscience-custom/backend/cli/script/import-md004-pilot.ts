import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/first-md-20261001/pilot-004-delivery-20261002')
const original = await Bun.file(path.join(root, 'reports/first-md-20261001/delivery-20261002/platform-receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'manifest.json')).json()
const audit = await Bun.file(path.join(folder, 'MD_004_AUDIT.json')).json()
if (audit.status !== 'RAW_TRAJECTORY_INTEGRITY_VERIFIED' || audit.frames !== 100 || audit.production_ps !== 1000 ||
  audit.binding_stability_established !== false || manifest.verified_trajectory_count !== 1) throw Error('Unexpected MD evidence')
for (const [filename, digest] of Object.entries(manifest.files)) {
  const actual = new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest('hex')
  if (actual !== digest) throw Error('MD delivery changed: ' + filename)
}
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of [...Object.keys(manifest.files), 'manifest.json']) {
    const mime = filename.endsWith('.md') ? 'text/markdown' : filename.endsWith('.csv') ? 'text/csv' :
      filename.endsWith('.png') ? 'image/png' : filename.endsWith('.svg') ? 'image/svg+xml' :
        filename.endsWith('.zip') ? 'application/zip' : 'application/json'
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: mime.startsWith('image/') ? 'image' : filename.endsWith('.md') ? 'document' : filename.endsWith('.csv') ? 'table' : 'data',
      mimeType: mime, captureQuality: 'declared',
      title: filename.endsWith('.md') ? '首个共价 MD · 004 · 1 ns 轨迹已验收' :
        filename.endsWith('.png') ? '004 MD · RMSD、共价键、温度与密度曲线' : '004 MD 试运行 · ' + filename }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, verified_trajectory_count: 1,
    binding_stability_established: false }, null, 2))
}
console.log(JSON.stringify({ verified_trajectory_count: 1, wrote_platform: process.argv.includes('--apply') }))
