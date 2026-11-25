import { createMemo, createResource, For, onCleanup, Show } from "solid-js"
import { createStore } from "solid-js/store"
import type { DiscoveryResult } from "@synsci/util/discovery-result"
import { useSDK } from "@/context/sdk"
import { verifyStructure } from "@/artifacts/candidate-structure"
import { format } from "./model"

export default function PublishedMD(props: { result: DiscoveryResult; candidate: string }) {
  const sdk = useSDK()
  const [state, setState] = createStore({ kind: "rmsd" })
  const candidate = createMemo(() => props.result.candidates.find(c => c.id === props.candidate))
  const charts = createMemo(() => props.result.charts.filter(c => c.candidate === props.candidate))
  const reports = createMemo(() => props.result.reports.filter(r => ["SCIENTIFIC_REVIEW.md", "analysis-bundle.zip", "system-summary.csv", "batch-summary.png"].includes(r.asset.name) && r.asset.storage === "artifact"))
  const steps = createMemo(() => [{ label: "轨迹验收", complete: (candidate()?.md.independentReplicas ?? 0) > 0 }, { label: "指标分析", complete: (candidate()?.md.metrics.length ?? 0) > 0 },
    { label: "图表接入", complete: charts().length > 0 }, { label: "报告归档", complete: reports().length > 0 }])
  const selected = createMemo(() => charts().find(c => c.kind === state.kind) ?? charts()[0])
  const names: Record<string, string> = { rmsd: "RMSD", retention: "口袋保持", contacts: "残基接触", hbonds: "氢键占有率", rmsf: "RMSF", physical: "体系与共价几何", blocks: "分块与重复差异" }
  function Figure(props: { chart: DiscoveryResult["charts"][number] }) {
    let url = ""
    let disposed = false
    const [content] = createResource(() => JSON.stringify(props.chart.asset), async key => {
      try {
        const asset = JSON.parse(key) as DiscoveryResult["charts"][number]["asset"]
        const response = asset.storage === "artifact"
          ? await sdk.request(`/file/artifact-store/${encodeURIComponent(asset.id)}/raw`, { signal: AbortSignal.timeout(30000) }, { versionID: asset.version })
          : await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(30000) }, { path: asset.path })
        if (!response.ok) throw Error(`图表读取失败（${response.status}）`)
        const bytes = asset.storage === "artifact" ? new Uint8Array(await response.arrayBuffer()) : await (async () => {
          const file = await response.json() as { content: string; encoding?: string; truncated?: boolean }
          if (file.truncated) throw Error("图表文件读取不完整")
          return file.encoding === "base64" ? Uint8Array.from(atob(file.content), character => character.charCodeAt(0)) : new TextEncoder().encode(file.content)
        })()
        await verifyStructure(bytes, asset.sha256)
        if (disposed) return { src: "", error: "" }
        if (url) URL.revokeObjectURL(url)
        url = `data:${asset.mediaType};base64,${btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(""))}`
        return { src: url, error: "" }
      } catch (error) { return { src: "", error: String(error) } }
    })
    onCleanup(() => { disposed = true; if (url) URL.revokeObjectURL(url) })
    return <figure class="md-analysis-figure" data-md-chart={props.chart.id}>
      <Show when={!content.loading && content()?.src} fallback={<div class="md-analysis-placeholder" role="status">{content.loading ? "校验并加载分析图…" : content()?.error}</div>}>
        <a href={content()?.src} target="_blank" rel="noreferrer" aria-label="打开原尺寸图表"><img src={content()?.src} alt={props.chart.title} /></a>
      </Show>
      <figcaption><strong>{props.chart.title}</strong><p>{props.chart.caption}</p></figcaption>
    </figure>
  }
  return <div class="discovery-md"><div class="md-analysis-heading"><div><span class="discovery-eyebrow">MOLECULAR DYNAMICS</span><h3>重复 MD · 轨迹与分析</h3><p>{candidate()?.md.summary}</p></div><span class="md-analysis-status">{candidate()?.md.state === "ready" ? "计算与分析已完成" : "部分证据已验收 · 仍需复核"}</span></div>
    <div class="discovery-md-summary"><For each={candidate()?.md.metrics}>{metric => <div><span>{metric.label}</span><strong>{format(metric.value)} <small>{metric.uncertainty ? `± ${format(metric.uncertainty.value)} ` : ""}{metric.unit}</small></strong><p>{metric.uncertainty ? `重复间 SD · n = ${metric.uncertainty.independentN}` : `已验收独立重复 · n = ${candidate()?.md.independentReplicas ?? 0}`}</p><p>{metric.definition}</p></div>}</For></div>
    <div class="md-analysis-steps"><For each={steps()}>{step => <div classList={{ complete: step.complete }}><span>{step.complete ? "✓" : "—"}</span><b>{step.label}</b><small>{step.complete ? "已完成" : "尚未接入"}</small></div>}</For></div>
    <div class="md-analysis-toolbar"><div role="tablist" aria-label="MD 分析图表"><For each={charts()}>{chart => <button role="tab" aria-selected={selected()?.id === chart.id} classList={{ selected: selected()?.id === chart.id }} onClick={() => setState("kind", chart.kind)}>{names[chart.kind] ?? chart.title}</button>}</For></div></div>
    <Show when={selected()} keyed>{chart => <Figure chart={chart} />}</Show>
    <p class="md-analysis-source">{candidate()?.note} {charts().length ? "图表使用已归档的轨迹分析结果，加载时核验 SHA-256。点击图表可查看原尺寸。" : "本候选的轨迹与分析状态已核验；分析曲线尚未接入此工作台。"}</p>
    <div class="md-analysis-actions"><For each={reports()}>{report => <Show when={report.asset.storage === "artifact" ? report.asset : undefined}>{asset => <a href={sdk.request.url(`/file/artifact-store/${asset().id}/raw`, { versionID: asset().version })} target="_blank" rel="noreferrer">{report.asset.name === "SCIENTIFIC_REVIEW.md" ? "科学复核说明" : report.asset.name === "analysis-bundle.zip" ? "完整分析包 · PNG / SVG / 数据" : report.asset.name === "system-summary.csv" ? "候选汇总数据" : "候选对比总图"} ↗</a>}</Show>}</For></div>
  </div>
}
