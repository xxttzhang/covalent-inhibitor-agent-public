import { createResource, lazy, Show, Suspense } from "solid-js"
import { useSDK } from "@/context/sdk"
import { candidateStructures, verifyStructure } from "./candidate-structure"
import { gzipPreview } from "./gzip-preview"
import { maestroGeometry } from "./maestro-preview"
import { denovoGeometry, type BondSelection } from './denovo-structure'
import { targetContext } from '@synsci/util/screening-target'

const Structure = lazy(() => import("@/science/renderers/molecular/ProteinStructure"))

export default function CandidateStructurePreview(props: { row: Record<string, string>; method: "af3" | "covdock" | "generated"; compact?: boolean; bond?: BondSelection }) {
  const sdk = useSDK()
  const [content] = createResource(() => ({ row: props.row, method: props.method, bond: props.bond }), async ({ row, method, bond }) => {
    const ref = candidateStructures(row).find((value) => value.method === method)
    if (!ref) throw new Error("此候选尚未登记可验证的三维结构。")
    const response = await sdk.request(`/file/artifact-store/${encodeURIComponent(ref.id)}`)
    if (!response.ok) throw new Error(`无法读取结构记录 (${response.status})`)
    const detail = await response.json()
    const version = detail.versions?.find((value: { id: string }) => value.id === ref.version)
    if (!version || version.sha256 !== ref.sha256 || version.size > 128 * 1024 * 1024)
      throw new Error("结构版本或校验值与候选登记不符。")
    if (method === "af3" && !/\.cif$/i.test(version.filename)) throw new Error("AF3 结构格式不支持。")
    if (method === "covdock" && !/\.(mae|maegz)$/i.test(version.filename)) throw new Error("CovDock 结构格式不支持。")
    if (method === 'generated' && !/\.pdb$/i.test(version.filename)) throw new Error('生成结构格式不支持。')
    const url = sdk.request.url(`/file/artifact-store/${encodeURIComponent(ref.id)}/raw`, { versionID: ref.version })
    const raw = await fetch(url)
    if (!raw.ok) throw new Error(`无法读取结构 (${raw.status})`)
    const bytes = new Uint8Array(await raw.arrayBuffer())
    if (bytes.length !== version.size) throw new Error("结构文件大小与登记不符。")
    await verifyStructure(bytes, ref.sha256)
    const decoded = /\.maegz$/i.test(version.filename) ? await gzipPreview(bytes) : bytes
    const text = new TextDecoder("utf-8", { fatal: true }).decode(decoded)
    const denovo = method === 'covdock' && row.route === 'de-novo' && bond ? denovoGeometry(text, ref.pose!, row.compound_id, bond) : undefined
    const geometry = method === "covdock" ? denovo ?? maestroGeometry(text, ref.pose!) : undefined
    if (geometry && row.route === 'de-novo' && geometry.denovoId !== row.compound_id) throw new Error('对接构象身份不匹配。')
    if (geometry && row.route !== 'de-novo' && (geometry.compound !== row.compound_id || geometry.inputIndex !== row.input_sdf_index))
      throw new Error("CovDock 姿势与候选 ID / 输入索引不符，未显示其他分子的构象。")
    const target = method === 'covdock' && row.route === 'de-novo' && bond ? targetContext.parse(JSON.parse(row.covdock_target_context)) : undefined
    if (target && (target.artifact_sha256 !== ref.sha256 || target.task_id !== row.task_id || !bond || target.parameters?.chain !== bond.chain || target.parameters.residue !== bond.residue || target.parameters.residue_number !== bond.number || target.parameters.numbering !== 'structure')) throw new Error('对接位点记录与已校验结构不符。')
    const mapping = target && denovo && target.parameters?.receptor_sha256 ? {
      ...denovo.site, artifact_sha256: ref.sha256, receptor_sha256: target.parameters.receptor_sha256,
    } : undefined
    return { data: method === 'generated' ? { pdb: text } : { cif: geometry?.cif ?? text }, target, mapping, url, filename: version.filename, pose: ref.pose }
  })
  return <section data-candidate-structure>
    <Show when={!props.compact}><p>{props.row.compound_id} · {props.method === "af3" ? "AF3" : "CovDock-LO"} 原始坐标；独立查看，不是对齐叠合，也不新增评分或门控。</p>
    <p>三维加载后点击“结合口袋”。结构残基编号可能与标准序列不同；当前入口不推断标准位点映射。</p></Show>
    <Show when={content.loading}><p>正在校验并读取结构…</p></Show>
    <Show when={content.error}><p role="alert">{String(content.error)}</p></Show>
    <Show when={!content.loading && !content.error && content()} keyed>{(value) => <>
      <details open={!props.compact}><summary>结构来源与下载</summary><p>文件：{value.filename}{value.pose === undefined ? "" : ` · 原始姿势索引 ${value.pose}（从 0 开始）`}</p>
      <a href={value.url} download={value.filename}>下载原始结构</a></details>
      <Suspense fallback={<p>正在加载已有 Mol* 查看器…</p>}>
        <Structure kind="protein-structure" data={value.data} targetContext={value.target} targetMapping={value.mapping} height={560} autoPocket={props.compact} />
      </Suspense>
    </>}</Show>
  </section>
}
