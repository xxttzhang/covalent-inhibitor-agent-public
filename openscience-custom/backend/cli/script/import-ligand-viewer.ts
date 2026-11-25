// Native Results import only. No prompt, tool execution, engine or scheduler.
import path from "node:path"
import { ArtifactStore } from "../src/artifact/store"
import { Instance } from "../src/project/instance"
import { Session } from "../src/session"
import { maestroGeometry } from "../../../frontend/workspace/src/artifacts/maestro-preview"
import { gzipPreview } from "../../../frontend/workspace/src/artifacts/gzip-preview"
import { candidateResultsManifest } from './candidate-results-manifest'

const project = process.argv.indexOf('--directory')
if (project >= 0 && (!process.argv[project + 1] || process.argv[project + 1].startsWith('--'))) throw new Error('Project directory required')
const directory = project >= 0 ? path.resolve(process.argv[project + 1]) : path.resolve(import.meta.dir, "../../../..")
const sessionArg = process.argv.indexOf('--session')
const requestedSession = sessionArg >= 0 ? process.argv[sessionArg + 1] : undefined
if (sessionArg >= 0 && !/^ses_[A-Za-z0-9]+$/.test(requestedSession ?? '')) throw new Error('Native session ID required')
const argument = process.argv.indexOf('--manifest')
if (argument < 0 && !process.argv.includes('--legacy')) throw new Error('Provide --manifest for a new task; historical import requires explicit --legacy')
if (argument >= 0 && (!process.argv[argument + 1] || process.argv[argument + 1].startsWith('--'))) throw new Error('Manifest filename required')
const filename = argument >= 0 ? path.resolve(process.argv[argument + 1]) : path.join(directory, "science/reports/ligand_viewer_20260924/display_manifest.json")
const root = path.dirname(filename)
const manifest = await Bun.file(filename).json() as {
  schema?: string; task_id?: string; route?: string
  assets: Record<string, { path: string; source: string; sha256: string; size: number }>
  documents?: Record<string, { path: string; source: string; sha256: string; size: number; mime_type: string; title: string }>
  candidates: Record<string, string>[]
}
if (!manifest.candidates.length || manifest.candidates.length > 100) throw new Error('Candidate count outside 1..100')
if (new Set(manifest.candidates.map((r) => r.compound_id)).size !== manifest.candidates.length
  || new Set(manifest.candidates.map((r) => r.input_sdf_index)).size !== manifest.candidates.length) throw new Error('Duplicate candidate identity')
if (argument >= 0 && (!['candidate-results-v1', 'candidate-results-v2'].includes(manifest.schema ?? '')
  || !['library-first', 'ligand-first', 'de-novo'].includes(manifest.route ?? '')
  || !/^[A-Za-z0-9._-]{6,128}$/.test(manifest.task_id ?? ''))) throw new Error('Versioned task manifest required')
if (argument >= 0) candidateResultsManifest.parse(manifest)
const hash = (bytes: Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
for (const asset of Object.values(manifest.assets)) {
  const bytes = new Uint8Array(await Bun.file(asset.path).arrayBuffer())
  if (bytes.length !== asset.size || hash(bytes) !== asset.sha256) throw new Error(`Asset changed: ${asset.path}`)
}
for (const document of Object.values(manifest.documents ?? {})) {
  const bytes = new Uint8Array(await Bun.file(document.path).arrayBuffer())
  if (bytes.length !== document.size || hash(bytes) !== document.sha256) throw new Error(`Document changed: ${document.path}`)
}
for (const row of manifest.candidates) {
  if (argument >= 0 && !row.covdock_asset) continue // Failed/missing poses remain visible, never fabricated.
  const source = manifest.assets[row.covdock_asset]
  const bytes = await gzipPreview(new Uint8Array(await Bun.file(source.path).arrayBuffer()))
  const pose = maestroGeometry(new TextDecoder().decode(bytes), Number(row.covdock_structure_pose))
  if (pose.compound !== row.compound_id || pose.inputIndex !== row.input_sdf_index) throw new Error("Pose identity mismatch")
}
if (!process.argv.includes("--apply")) {
  console.log(JSON.stringify({ validated: true, candidates: manifest.candidates.length, assets: Object.keys(manifest.assets).length,
    documents: Object.keys(manifest.documents ?? {}).length, wrote_platform: false }))
  process.exit(0)
}
await Instance.provide({ directory, async fn() {
  const receipt = path.join(root, "platform_receipt.json")
  const prior = await Bun.file(receipt).exists() ? await Bun.file(receipt).json() : undefined
  if (prior && prior.project !== Instance.project.id) throw new Error("Receipt belongs to another project")
  if (argument >= 0 && prior && prior.task_id !== manifest.task_id) throw new Error('Receipt belongs to another task; use a separate manifest directory')
  if (prior && requestedSession && prior.session !== requestedSession) throw new Error('Receipt belongs to another session')
  const currentRoute = argument >= 0 ? manifest.route! : 'ligand-first'
  const routeLabel = currentRoute === 'library-first' ? 'Library-first' : currentRoute === 'de-novo' ? 'De novo' : 'Ligand-first'
  const session = prior || requestedSession ? await Session.get(prior?.session ?? requestedSession)
    : await Session.create({ title: `${routeLabel} · ${manifest.task_id ?? 'EGFR C797 历史结果'} · ${manifest.candidates.length} 个候选` })
  if (session.projectID !== Instance.project.id) throw new Error('Session belongs to another project')
  const saved: Record<string, ArtifactStore.Artifact> = {}
  let overviewArtifact: ArtifactStore.Artifact | undefined
  const persist = () => {
    const result = overviewArtifact?.id
    const baseURL = (process.env.OPENSCIENCE_PUBLIC_BASE_URL ?? `http://127.0.0.1:${process.env.OPENSCIENCE_PORT ?? "4096"}`).replace(/\/$/, "")
    const url = `${baseURL}/${Instance.project.id}/session/${session.id}${result ? `?result=${encodeURIComponent(result)}` : ''}`
    return Bun.write(receipt, JSON.stringify({ project: Instance.project.id, session: session.id, task_id: manifest.task_id ?? null,
      url, result_artifact_id: result ?? null, artifacts: saved, new_calculation: false }, null, 2) + "\n")
  }
  await persist()
  const existing = await ArtifactStore.list(Instance.project.id, "active")
  async function save(name: string, file: string, kind: string, title: string, mimeType: string) {
    const bytes = new Uint8Array(await Bun.file(file).arrayBuffer())
    const sha256 = hash(bytes)
    const match = existing.find((item) => item.current.sourcePath === file && item.current.sha256 === sha256 && item.current.sessionID === session.id)
    const artifact = match ?? await ArtifactStore.save({ projectID: Instance.project.id, sessionID: session.id, sourcePath: file,
      filename: name, kind, content: new Blob([bytes]), title, mimeType, captureQuality: "declared" })
    saved[name] = artifact
    await persist()
    return artifact
  }
  for (const [name, asset] of Object.entries(manifest.assets))
    await save(name, asset.path, "protein-structure", `${routeLabel} · ${name}`, name.endsWith(".maegz") ? "application/gzip" : "chemical/x-mmcif")
  const savedReports: Record<string, ArtifactStore.Artifact> = {}
  for (const [role, document] of Object.entries(manifest.documents ?? {})) {
    const artifact = await save(path.basename(document.path), document.path, "document",
      `${routeLabel} · ${document.title}`, document.mime_type)
    savedReports[role] = artifact
  }
  const rows = manifest.candidates.map((candidate) => {
    const row: Record<string, string> = { ...candidate, ...(argument >= 0 ? { result_scope: manifest.schema!, route: currentRoute, task_id: manifest.task_id! } : {}) }
    for (const method of ["af3", "covdock", "generated"]) {
      const artifact = saved[row[`${method}_asset`]]
      row[`${method}_artifact_id`] = artifact?.id ?? ""
      row[`${method}_artifact_version`] = artifact?.current.id ?? ""
      row[`${method}_artifact_sha256`] = artifact?.current.sha256 ?? ""
    }
    for (const [role, artifact] of Object.entries(savedReports)) {
      row[`${role}_artifact_id`] = artifact.id
      row[`${role}_artifact_version`] = artifact.current.id
      row[`${role}_artifact_sha256`] = artifact.current.sha256
    }
    return row
  })
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))]
  const quote = (value: string) => `"${String(value ?? "").replaceAll('"', '""')}"`
  const csv = [columns.map(quote).join(","), ...rows.map((row) => columns.map((key) => quote(row[key])).join(","))].join("\n") + "\n"
  const tableName = currentRoute === 'library-first' ? 'library_first_viewer_candidates.csv'
    : currentRoute === 'de-novo' ? 'de_novo_viewer_candidates.csv' : 'ligand_first_viewer_candidates.csv'
  const table = path.join(root, tableName)
  await Bun.write(table, csv)
  overviewArtifact = await save(path.basename(table), table, "table", `${routeLabel} · ${rows.length} 个候选 · 查看结构／结合口袋`, "text/csv")
  await persist()
  await save("display_manifest.json", filename, "file", `${routeLabel} · 原始结构来源与 SHA256`, "application/json")
  const report = path.join(root, "README.md")
  await Bun.write(report, `# Ligand-first 三候选结构查看\n\n在 Files / Results 打开 **Ligand-first · 3 个候选 · 查看结构／结合口袋**，选择候选，再点击 AF3 或 CovDock 结构按钮。加载后可点击“一键查看结合口袋”。\n\n- 原有 Mol* / RDKit 查看器；没有新对接、推理、打分或联合重排。\n- 两个历史候选（input59、65）提供 AF3 与 CovDock 独立视图，Gate A；新完成候选（input57）提供 CovDock 视图，仍为 Gate B2，缺少跨方法一致性评价。\n- CSV 保持历史源表顺序并附加新候选；不是三分子的优劣排名。\n- 导入的结构保留原始字节；CovDock 精确选择原文件姿势索引 16、7、4，并校验候选 ID 和输入索引。\n- 展示不是对齐叠合；Maestro 到 mmCIF 的转换仅用于几何显示，不新增拓扑或键级验证。\n- 当前入口未传递标准序列残基映射；AF3 与 CovDock 原始残基编号可能不同，需查看结构原始编号，不能把显示高亮当作位点验证。\n- 二维图来自表内 SMILES；计算门控不代表实验活性。\n\n来源及校验信息见 display_manifest.json。\n`)
  if (argument >= 0) await Bun.write(report, `# ${routeLabel} 候选结构报告\n\n任务：${manifest.task_id}；候选：${rows.length}。\n\n来自提交的版本化清单；未运行新计算、未重新排名。原始门控及缺失测量均保留，二维结构不是对接构象，计算评价不是实验活性证明。结构文件由 SHA256 与精确姿势索引校验。\n`)
  await save("README.md", report, "document", `${routeLabel} · 结构查看说明与证据边界`, "text/markdown")
  console.log(JSON.stringify({ project: Instance.project.id, session: session.id, receipt, artifacts: Object.keys(saved).length, new_calculation: false }))
} })
await Instance.disposeAll()
