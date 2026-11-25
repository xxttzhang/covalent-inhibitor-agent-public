import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/first-md-20261001/md-protocol-20261002')
const original = await Bun.file(path.join(root, 'reports/three-route-evaluation-20261001/platform-receipt.json')).json()
const manifest = await Bun.file(path.join(folder, 'delivery-manifest.json')).json()
for (const [filename, digest] of Object.entries(manifest.files)) {
  const actual = new Bun.CryptoHasher('sha256').update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest('hex')
  if (actual !== digest) throw Error('MD protocol file changed: ' + filename)
}
const check = await Bun.file(path.join(folder, 'parser-check.json')).json()
if (check.status !== 'INSTALLED_STAGE_PARSER_PASSED' || check.stages.length !== 7 || check.simulation_started) throw Error('Unexpected parser evidence')
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published; inspect receipt')
  const artifacts = []
  for (const filename of ['MD_PROTOCOL.md', 'pilot.msj', 'plan.json', 'parser-check.json', 'delivery-manifest.json']) {
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.md') ? 'text/markdown' : filename.endsWith('.msj') ? 'text/plain' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.md') ? '首批 MD · GPU 运行协议 · 配置解析通过' : '首批 MD 协议 · ' + filename }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project, session: original.session,
    url: original.url, artifacts, gpu_submitted: false }, null, 2))
}
console.log(JSON.stringify({ parser_passed: true, wrote_platform: process.argv.includes('--apply') }))
