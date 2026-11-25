import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/md-validation-20261003')
const original = await Bun.file(path.join(root, 'reports/first-md-20261001/batch-delivery-20261002/platform-receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'manifest.json')).json()
const crosscheck = await Bun.file(path.join(folder, 'MDANALYSIS_CROSSCHECK.json')).json()
if (manifest.verified_systems !== 4 || manifest.verified_frames !== 400 || manifest.HB_geometries !== 900 ||
  manifest.long_term_binding_stability_established !== false || manifest.new_GPU_jobs !== 0 ||
  crosscheck.results.length !== 4 || crosscheck.results.some((r: { status: string; frames: number }) =>
    r.status !== 'CROSSCHECK_PASSED' || r.frames !== 100)) throw Error('Unexpected validation evidence')
for (const [filename, digest] of Object.entries(manifest.files)) {
  const actual = new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest('hex')
  if (actual !== digest) throw Error('Validation delivery changed: ' + filename)
}
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of [...Object.keys(manifest.files), 'manifest.json']) {
    const mime = filename.endsWith('.md') ? 'text/markdown' : filename.endsWith('.csv') ? 'text/csv' :
      filename.endsWith('.png') ? 'image/png' : filename.endsWith('.svg') ? 'image/svg+xml' :
        filename.endsWith('.zip') ? 'application/zip' : filename.endsWith('.txt') ? 'text/plain' : 'application/json'
    const title = filename === 'MD_VALIDATION_REPORT.md' ? 'MD 验证 · 四体系短程复核与结论 · 2026-10-03' :
      filename === 'MD_VALIDATION_RMSD.png' ? 'MD 验证 · 整体配体与主要环系位移' :
        filename === 'MD_VALIDATION_CONTACTS.png' ? 'MD 验证 · 接触保持与氢键阈值敏感性' : 'MD 验证 · ' + filename
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: mime.startsWith('image/') ? 'image' : filename.endsWith('.md') ? 'document' : filename.endsWith('.csv') ? 'table' : 'data',
      mimeType: mime, captureQuality: 'declared', title }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, verified_systems: 4,
    long_term_binding_stability_established: false }, null, 2))
}
console.log(JSON.stringify({ verified_systems: 4, wrote_platform: process.argv.includes('--apply') }))
