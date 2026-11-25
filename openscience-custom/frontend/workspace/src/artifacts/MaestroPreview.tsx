import { createMemo, createResource, For, lazy, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import { maestroGeometry, maestroOffsets } from "./maestro-preview"
import { gzipPreview } from "./gzip-preview"
import type { TargetContext } from "@synsci/util/screening-target"

const Structure = lazy(() => import("@/science/renderers/molecular/ProteinStructure"))

async function bounded(stream: ReadableStream<Uint8Array>, limit: number) {
  const reader = stream.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > limit) throw new Error("Structure preview size limit exceeded")
      chunks.push(next.value)
    }
  } finally {
    await reader.cancel()
  }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return bytes
}

export default function MaestroPreview(props: { url: string; compressed: boolean; targetContext?: TargetContext }) {
  const [state, setState] = createStore({ pose: 0 })
  const [source] = createResource(() => [props.url, props.compressed] as const, async ([url, compressed]) => {
    const response = await fetch(url)
    if (!response.ok || !response.body) throw new Error(`Maestro download failed (${response.status})`)
    const bytes = await bounded(response.body, 128 * 1024 * 1024)
    const decoded = compressed ? await gzipPreview(bytes) : bytes
    const text = new TextDecoder("utf-8", { fatal: true }).decode(decoded)
    setState("pose", 0)
    return { text, offsets: maestroOffsets(text) }
  })
  const geometry = createMemo(() => {
    const value = source()
    if (!value) return
    try { return { result: maestroGeometry(value.text, state.pose, value.offsets) } }
    catch (error) { return { error: error instanceof Error ? error.message : String(error) } }
  })
  return <div data-maestro-preview>
    <p>Maestro 原始坐标几何预览；不验证键级、拓扑或立体化学，不新增评分。下载保留原始文件。</p>
    <Show when={source.loading}><p>正在读取结构…</p></Show>
    <Show when={source.error}><p role="alert">{String(source.error)}</p></Show>
    <Show when={source()}>{(value) => <label>姿势索引（从 0 开始，不代表排名）
      <select aria-label="Maestro pose index" value={state.pose} onChange={(event) => setState("pose", Number(event.currentTarget.value))}>
        <For each={value().offsets}>{(_, index) => <option value={index()}>{index()}</option>}</For>
      </select>
    </label>}</Show>
    <Show when={geometry()?.error}><p role="alert">{geometry()?.error}</p></Show>
    <Show when={geometry()?.result}>{(value) => <>
      <p data-maestro-identity>候选索引：{value().inputIndex ?? "未提供"} · {value().compound ?? "未提供"} · {value().atoms.length} 个原子</p>
      <Suspense fallback={<p>正在加载三维查看器…</p>}>
        <Structure kind="protein-structure" data={{ cif: value().cif }} height={560} targetContext={props.targetContext} />
      </Suspense>
    </>}</Show>
  </div>
}
