import path from 'node:path'
import { ArtifactStore } from '../src/artifact/store'

const root = path.resolve(import.meta.dir, '../../../..')
const folder = path.join(root, 'reports/md-replicas-launch-20261008-v1')
const original = await Bun.file(path.join(root, 'reports/md-replicas-20261003-v1/platform-receipt.json')).json()
const launch = await Bun.file(path.join(folder, 'launch-summary.json')).json()
if (launch.submitted_jobs !== 4 || launch.runs.length !== 4 || launch.scientific_replicas_completed !== 0)
  throw Error('Unexpected initial launch snapshot')
for (const run of launch.runs) {
  const receipt = await Bun.file(path.join(folder, run.job_name + '.receipt.json')).json()
  if (!receipt.success || receipt.data.name !== run.job_name || receipt.data.status !== 'created')
    throw Error('Missing real submission receipt')
}
if (process.argv.includes('--apply')) {
  if (await Bun.file(path.join(folder, 'platform-receipt.json')).exists()) throw Error('Already published')
  const artifacts = []
  for (const filename of ['MD_LAUNCH_STATUS.md', 'launch-summary.json', 'deployment-verification.json']) {
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: filename.endsWith('.md') ? 'document' : 'data',
      mimeType: filename.endsWith('.md') ? 'text/markdown' : 'application/json',
      captureQuality: 'declared', title: filename.endsWith('.md') ? '重复 MD · 首轮四体系已提交 · 2026-10-08' : 'MD 启动证据 · ' + filename }))
  }
  await Bun.write(path.join(folder, 'platform-receipt.json'), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, submitted_jobs: 4 }, null, 2))
}
console.log(JSON.stringify({ submitted_jobs: 4, wrote_platform: process.argv.includes('--apply') }))
