import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/first-md-20261001/batch-delivery-20261002')
const original = await Bun.file(path.join(root, 'reports/first-md-20261001/delivery-20261002/platform-receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'manifest.json')).json()
const audits = await Bun.file(path.join(folder, 'MD_FIRST_BATCH_AUDITS.json')).json()
const evaluation = await Bun.file(path.join(folder, 'THREE_ROUTE_WITH_MD.json')).json()
if (manifest.verified_trajectory_count !== 4 || manifest.total_saved_frames !== 400 ||
  manifest.three_route_candidate_rows !== 18 || manifest.pose_grades_unchanged !== true ||
  manifest.binding_stability_established !== false || evaluation.records.length !== 18 ||
  Object.keys(audits).length !== 4) throw Error('Unexpected MD batch evidence')
for (const name of ['denovo_001', 'denovo_004', 'denovo_007', 'control_afatinib']) {
  const audit = audits[name]
  if (audit?.status !== 'RAW_TRAJECTORY_INTEGRITY_VERIFIED' || audit.frames !== 100 ||
    audit.production_ps !== 1000 || audit.binding_stability_established !== false) throw Error('Invalid audit: ' + name)
}
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
      title: filename.endsWith('.md') ? '首批共价 MD · 三候选与阿法替尼 · 四体系已验收' :
        filename.endsWith('.png') ? '首批 MD · 四体系 RMSD、共价键、温度与密度' :
          filename === 'THREE_ROUTE_WITH_MD.csv' ? '三路线统一评价 · 18 候选 · 首批 MD 已同步' : '首批 MD · ' + filename }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, verified_trajectory_count: 4,
    binding_stability_established: false }, null, 2))
}
console.log(JSON.stringify({ verified_trajectory_count: 4, wrote_platform: process.argv.includes('--apply') }))
