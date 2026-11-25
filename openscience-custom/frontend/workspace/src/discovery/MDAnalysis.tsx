import { projectPath, type RoutePresentation } from "@synsci/util/discovery-config"
import { createMemo, createResource, For, onCleanup, onMount, Show } from "solid-js"
import { createStore } from "solid-js/store"
import { z } from "zod"
import { useSDK } from "@/context/sdk"
import { stale, time } from "./model"

const artifact = z.object({ path: projectPath, sha256: z.string().regex(/^[a-f0-9]{64}$/) })
const schema = z.object({ schema: z.literal("md-analysis-state-v1"), project: z.string(), collectedAt: z.string(),
  signature: z.string(), phase: z.enum(["waiting", "validation", "analysis", "rendering", "report", "ready", "failed"]),
  count: z.number(), expected: z.number().int().positive(), error: z.string(), finishedAt: z.string(),
  charts: z.array(artifact.extend({ system: z.string(), kind: z.string(), title: z.string(), caption: z.string() })),
  bundle: artifact.nullable() })
const steps = [{ id: "validation", label: "轨迹验收" }, { id: "analysis", label: "指标分析" },
  { id: "rendering", label: "图表生成" }, { id: "report", label: "报告归档" }]
const kinds = [{ id: "rmsd", label: "RMSD 与对照" }, { id: "contacts", label: "接触热图" },
  { id: "hbonds", label: "氢键占有率" }, { id: "sampling", label: "采样诊断" }, { id: "physical", label: "物理质检" }]

export default function MDAnalysis(props: { candidate: string; settings: NonNullable<RoutePresentation["md"]> }) {
  const sdk = useSDK()
  const [state, setState] = createStore({ data: undefined as z.infer<typeof schema> | undefined,
    kind: "rmsd", error: "", busy: false, zoom: false, downloading: false, downloadError: "", now: Date.now() })
  const sync = async () => {
    if (state.busy) return
    setState("busy", true)
    try {
      const response = await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(12000) }, { path: props.settings.statePath })
      if (!response.ok) throw Error("自动分析服务暂不可用")
      const envelope = await response.json()
      const data = schema.parse(JSON.parse(envelope.content))
      if (data.project !== sdk.projectID || data.expected !== props.settings.expectedReplicas) throw Error("分析结果与当前项目不一致")
      setState({ data, error: "" })
    } catch { setState("error", "未能获取最新分析状态；已显示的图表保留供查阅。") }
    finally { setState({ busy: false, now: Date.now() }) }
  }
  onMount(() => {
    void sync()
    const timer = setInterval(() => { setState("now", Date.now()); if (!document.hidden) void sync() }, 15000)
    onCleanup(() => clearInterval(timer))
  })
  const chart = createMemo(() => state.data?.charts.find(c => c.system === props.candidate && c.kind === state.kind))
  const offline = createMemo(() => !!state.error || !!state.data && stale(state.data.collectedAt, state.now))
  const active = createMemo(() => state.data?.phase === "ready" ? 4 : steps.findIndex(s => s.id === state.data?.phase))
  const bytes = async (ref: z.infer<typeof artifact>) => {
    if (!ref.path.startsWith(props.settings.artifactPrefix + "/")) throw Error("文件不属于当前分析结果目录")
    const response = await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(30000) }, { path: ref.path })
    if (!response.ok) throw Error("文件读取失败")
    const body = await response.json()
    if (body.encoding !== "base64" || body.truncated) throw Error("文件不完整")
    const data = Uint8Array.from(atob(body.content), c => c.charCodeAt(0))
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", data)), c => c.toString(16).padStart(2, "0")).join("")
    if (hash !== ref.sha256) throw Error("图表校验未通过")
    return { data, base64: body.content as string }
  }
  // Stable key prevents downloading the same image again on status heartbeats.
  const [figure] = createResource(() => chart() ? JSON.stringify({ path: chart()!.path, sha256: chart()!.sha256 }) : undefined,
    async key => {
      try { return { src: "data:image/png;base64," + (await bytes(JSON.parse(key))).base64, error: "" } }
      catch (error) { return { src: "", error: error instanceof Error ? error.message : "图表加载失败" } }
    })
  const download = async () => {
    const ref = state.data?.bundle
    if (!ref || state.downloading) return
    setState({ downloading: true, downloadError: "" })
    try {
      const result = await bytes(ref)
      const url = URL.createObjectURL(new Blob([result.data], { type: "application/zip" }))
      const link = document.createElement("a")
      link.href = url; link.download = "md-analysis-bundle.zip"; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 10000)
    } catch { setState("downloadError", "报告包下载失败，请稍后重试。") }
    finally { setState("downloading", false) }
  }
  return <section class="md-analysis" aria-label="MD 自动分析与图表">
    <header class="md-analysis-heading"><div><span class="md-analysis-eyebrow">TRAJECTORY → INSIGHT</span><h3>轨迹自动分析</h3></div>
      <span classList={{ "md-analysis-status": true, warning: offline() || state.data?.phase === "failed" }}>
        {offline() ? "连接待恢复" : state.data?.phase === "ready" ? "图表与报告已生成" : state.data?.phase === "failed" ? "分析失败" : state.data?.phase === "waiting" ? "等待验收数据" : "分析中"}</span></header>
    <div class="md-analysis-steps"><For each={steps}>{(step, i) => <div classList={{ complete: i() < active(), current: i() === active() }}>
      <span>{i() < active() ? "✓" : String(i() + 1).padStart(2, "0")}</span><b>{step.label}</b>
      <small>{i() < active() ? "已完成" : i() === active() ? "进行中" : "等待"}</small></div>}</For></div>
    <Show when={offline()}><p class="md-analysis-notice" role="status">{state.error || "分析状态超过 60 秒未更新，图表是上次已核验结果。"}</p></Show>
    <Show when={state.data?.phase === "failed"}><div class="md-analysis-notice" role="alert">自动分析未完成，未发布本次图表。<details><summary>查看原因</summary><pre>{state.data?.error}</pre></details></div></Show>
    <Show when={state.data?.phase === "waiting"}><p>已收到 {state.data?.count} / {state.data?.expected} 条验收记录。全部齐全后自动分析。</p></Show>
    <Show when={state.data?.phase === "ready"}>
      <div class="md-analysis-toolbar"><div role="tablist" aria-label="轨迹图类型"><For each={kinds}>{kind => <button role="tab" aria-selected={state.kind === kind.id} classList={{ selected: state.kind === kind.id }} onClick={() => setState("kind", kind.id)}>{kind.label}</button>}</For></div></div>
      <figure class="md-analysis-figure" aria-busy={figure.loading}>
        <Show when={!figure.loading} fallback={<div class="md-analysis-placeholder">正在读取并校验图表…</div>}>
          <Show when={figure()?.src} fallback={<div class="md-analysis-placeholder" role="alert">{figure()?.error || "此候选尚无对应图表"}</div>}>
            <img src={figure()?.src} alt={`${props.candidate} · ${chart()?.title}。${chart()?.caption}`} />
          </Show>
        </Show>
        <figcaption><strong>{chart()?.title}</strong><p>{chart()?.caption}</p></figcaption>
      </figure>
      <div class="md-analysis-actions"><button onClick={() => setState("zoom", true)} disabled={!figure()?.src || figure.loading}>放大查看 ↗</button>
        <a href={figure()?.src} download={`${props.candidate}-${state.kind}.png`}>下载当前图</a>
        <button onClick={() => void download()} disabled={state.downloading}>{state.downloading ? "正在核验下载…" : "下载完整报告与源数据 ↓"}</button></div>
      <Show when={state.downloadError}><p role="alert">{state.downloadError}</p></Show>
      <div class="md-analysis-caveats"><div><b>采样仍不足</b><p>{props.settings.samplingNote}</p></div>
        <div><b>RMSF · 待补分析</b><p>{props.settings.rmsfNote}</p></div></div>
      <p class="md-analysis-source">{props.settings.sourceNote}生成于 {state.data?.finishedAt ? time(state.data.finishedAt) : "—"}。</p>
    </Show>
    <Show when={state.zoom}><dialog class="md-analysis-overlay" aria-label="轨迹图放大查看"
      ref={element => queueMicrotask(() => element.showModal())}
      onCancel={event => { event.preventDefault(); setState("zoom", false) }}>
      <button autofocus onClick={() => setState("zoom", false)}>关闭 ×</button><img src={figure()?.src} alt={chart()?.caption} /></dialog></Show>
  </section>
}
