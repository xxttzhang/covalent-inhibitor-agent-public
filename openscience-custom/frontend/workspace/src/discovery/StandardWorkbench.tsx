import { createMemo, createResource, ErrorBoundary, For, lazy, onCleanup, onMount, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import { useSDK } from "@/context/sdk"
import { parseResult, type DiscoveryResult } from "@synsci/util/discovery-result"
import type { DiscoveryProject } from "@synsci/util/discovery-config"
import { verifyStructure } from "@/artifacts/candidate-structure"
import BackToChat from "./BackToChat"
import RouteNavigation from "./RouteNavigation"
import type { JSX } from "solid-js"
import { format, time } from "./model"
const Chem2D = lazy(() => import("@/science/renderers/molecular/Chem2D"))
const Structure = lazy(() => import("@/science/renderers/molecular/ProteinStructure"))
type Asset = DiscoveryResult["reports"][number]["asset"]

export default function StandardWorkbench(props: { config: DiscoveryProject; route: keyof DiscoveryProject["routes"]; children?: JSX.Element }) {
  const sdk = useSDK()
  const settings = () => props.config.routes[props.route]
  const [state, setState] = createStore({ data: undefined as DiscoveryResult | undefined, error: "", selected: "", stage: "", tab: "structure", busy: false })
  const sync = async () => {
    if (state.busy) return
    setState("busy", true)
    try {
      const response = await sdk.request("/file/content", { signal: AbortSignal.timeout(12000), cache: "no-store" }, { path: settings().dataPath! })
      if (!response.ok) throw Error("读取结果失败")
      const body = await response.json()
      if (!body.content?.trim()) { setState("error", ""); return }
      const data = parseResult(JSON.parse(body.content), { project: props.config.project, route: props.route, target: props.config.target!.id })
      setState({ data, error: "" })
      if (!data.candidates.some(c => c.id === state.selected)) setState("selected", data.candidates[0]?.id ?? "")
      if (!data.stages.some(s => s.id === state.stage)) setState("stage", data.current)
    } catch (error) { setState("error", String(error)) } finally { setState("busy", false) }
  }
  onMount(() => { void sync(); const timer = setInterval(() => { if (!document.hidden) void sync() }, 15000); onCleanup(() => clearInterval(timer)) })
  const candidate = createMemo(() => state.data?.candidates.find(c => c.id === state.selected))
  const phase = createMemo(() => state.data?.stages.find(s => s.id === state.stage))
  const bytes = async (ref: Asset) => {
    const response = ref.storage === "artifact" ? await sdk.request(`/file/artifact-store/${encodeURIComponent(ref.id)}/raw`, { signal: AbortSignal.timeout(30000) }, { versionID: ref.version }) : await sdk.request("/file/content", { signal: AbortSignal.timeout(30000) }, { path: ref.path })
    if (!response.ok) throw Error("文件读取失败")
    const data = ref.storage === "artifact" ? new Uint8Array(await response.arrayBuffer()) : await response.json().then(body => {
      if (body.truncated) throw Error("文件被截断")
      return body.encoding === "base64" ? Uint8Array.from(atob(body.content), c => c.charCodeAt(0)) : new TextEncoder().encode(body.content)
    })
    await verifyStructure(data, ref.sha256)
    return data
  }
  const preview = createMemo(() => candidate()?.structures.find(s => ["pdb", "cif"].includes(s.format)))
  const [structure] = createResource(() => preview() ? JSON.stringify(preview()) : undefined, async key => {
    try { const ref = JSON.parse(key) as NonNullable<ReturnType<typeof preview>>; return { data: { [ref.format]: new TextDecoder().decode(await bytes(ref.asset)) }, error: "" } }
    catch (error) { return { data: undefined, error: String(error) } }
  })
  function Figure(props: { chart: DiscoveryResult["charts"][number] }) {
    const [content] = createResource(() => JSON.stringify(props.chart.asset), async key => {
      try { const ref = JSON.parse(key) as Asset; const data = await bytes(ref); return { src: URL.createObjectURL(new Blob([data as Uint8Array<ArrayBuffer>], { type: ref.mediaType })), error: "" } }
      catch (error) { return { src: "", error: String(error) } }
    })
    onCleanup(() => { const src = content()?.src; if (src) URL.revokeObjectURL(src) })
    return <figure><h3>{props.chart.title}</h3><Show when={content()?.src} fallback={<p>{content()?.error || "校验图表…"}</p>}><img src={content()?.src} alt={props.chart.title} style={{ width: "100%" }} /></Show><figcaption>{props.chart.caption}</figcaption></figure>
  }
  const download = async (ref: Asset) => {
    try { const data = await bytes(ref); const url = URL.createObjectURL(new Blob([data as Uint8Array<ArrayBuffer>], { type: ref.mediaType })); const link = document.createElement("a"); link.href = url; link.download = ref.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000) }
    catch (error) { setState("error", String(error)) }
  }
  return <main class="discovery" data-discovery-workbench><div class="discovery-inner">
    <header class="discovery-top"><BackToChat project={sdk.projectID} /><span>共价药物发现 / 路线工作台</span><span class="discovery-top-tag">{props.config.target?.label}</span></header>
    <RouteNavigation project={sdk.projectID} active={props.route} />
    {props.children}
    <section class="discovery-hero"><div><span class="discovery-eyebrow">STRUCTURE-BASED DISCOVERY</span><h1>{settings().title}<span>{settings().name}</span></h1><p>{settings().subtitle}</p></div><div class="discovery-sync"><span><i />{state.error ? "同步需要检查" : state.data ? "任务记录自动同步" : "等待计算结果"}</span><small>{state.data ? `读取于 ${time(state.data.collectedAt)}` : "当前项目"}</small><button onClick={() => void sync()}>↻ 刷新进度</button></div></section>
    <Show when={state.error}><div class="discovery-alert" role="alert">{state.error}</div></Show>
    <Show when={state.data} fallback={<section class="discovery-progress"><div class="refinement-md-empty"><h3>待运行</h3><p>靶点已配置，尚无本路线的有效计算结果。</p></div><div class="discovery-empty">尚无本项目候选分子</div></section>}>{data => <>
      <section class="discovery-stats"><For each={data().stats}>{stat => <div><span>{stat.label}</span><strong>{stat.value}<small> {stat.unit}</small></strong><p>{stat.note}</p></div>}</For></section>
      <section class="discovery-progress"><div class="discovery-section-title"><h2>研究进程</h2><span>点击阶段查看详情</span></div><div class="discovery-steps"><For each={data().stages}>{(step, index) => <button class="discovery-step" classList={{ "is-done": step.state === "completed", "is-current": step.state === "running", "is-blocked": ["failed", "blocked"].includes(step.state), "is-selected": state.stage === step.id }} onClick={() => setState("stage", step.id)}><span class="discovery-step-number">{step.state === "completed" ? "✓" : index() + 1}</span><b>{step.label}</b><small>{step.count}</small><em>{step.status}</em></button>}</For></div><Show when={phase()}>{step => <div class="discovery-phase"><div class="discovery-phase-marker" /><div><h3>{step().title}</h3><p>{step().detail}</p><div class="discovery-phase-facts"><For each={step().facts}>{fact => <span>{fact}</span>}</For></div></div></div>}</Show></section>
      <section class="discovery-candidates"><div class="discovery-section-title"><h2>候选分子 <span>{data().candidates.length}</span></h2><p>{data().notice}</p></div><div class="discovery-cards"><For each={data().candidates}>{entry => <article class="discovery-card" classList={{ "is-selected": state.selected === entry.id }} data-discovery-candidate={entry.id}><button class="discovery-card-select" onClick={() => setState("selected", entry.id)}><span><small>{settings().name}</small><strong>{entry.label}</strong></span><span class={`discovery-badge ${entry.tone}`}>{entry.badge}</span></button><div class="discovery-molecule"><ErrorBoundary fallback={<code>{entry.smiles}</code>}><Suspense fallback={<p>绘制分子结构…</p>}><Chem2D kind="chem-2d" data={{ smiles: entry.smiles }} height={175} /></Suspense></ErrorBoundary></div><div class="discovery-card-values"><For each={entry.metrics.slice(0, 2)}>{metric => <div><span>{metric.label}</span><b>{format(metric.value)} <small>{metric.unit}</small></b></div>}</For></div></article>}</For></div></section>
      <Show when={candidate()}>{entry => <section class="discovery-detail"><div class="discovery-detail-header"><h2>{entry().label}</h2><div class="discovery-tabs"><For each={[["structure", "结合构象"], ["md", "MD 证据"], ["records", "研究记录"]]}>{tab => <button aria-pressed={state.tab === tab[0]} onClick={() => setState("tab", tab[0])}>{tab[1]}</button>}</For></div></div>
        <Show when={state.tab === "structure"}><div class="discovery-structure-grid"><div class="discovery-viewer"><Show when={structure()?.data} fallback={<div class="discovery-empty">{structure()?.error || "暂无可预览的 PDB / CIF 结构；已登记文件可从研究记录下载。"}</div>}>{content => <Suspense fallback={<p>加载三维结构…</p>}><Structure kind="protein-structure" data={content()} height={560} /></Suspense>}</Show></div><aside class="discovery-evidence"><h3>当前证据</h3><p>{entry().note}</p><code>{entry().smiles}</code></aside></div></Show>
        <Show when={state.tab === "md"}><div class="discovery-md"><h3>{entry().md.state === "pending" ? "等待本候选的正式轨迹" : "分子动力学证据"}</h3><p>{entry().md.summary}</p><p>独立重复：{entry().md.independentReplicas}</p><div class="discovery-md-summary"><For each={entry().md.metrics}>{metric => <div><span>{metric.label}</span><strong>{format(metric.value)} <small>{metric.unit}</small></strong><p>{metric.definition}</p><Show when={metric.uncertainty}>{uncertainty => <p>{uncertainty().kind}: {uncertainty().value} · n = {uncertainty().independentN}</p>}</Show></div>}</For></div><For each={data().charts.filter(c => c.candidate === entry().id)}>{chart => <Figure chart={chart} />}</For></div></Show>
        <Show when={state.tab === "records"}><div class="discovery-records"><For each={entry().structures}>{ref => <p><button class="discovery-link" onClick={() => void download(ref.asset)}>{ref.method} · 下载结构 ↗</button></p>}</For><For each={data().reports}>{report => <p><button class="discovery-link" onClick={() => void download(report.asset)}>{report.title} ↗</button></p>}</For></div></Show>
      </section>}</Show><footer class="discovery-footer"><span>科研证据可追溯</span><p>证据更新于 {time(data().evidenceUpdatedAt)}</p><small>页面查看不提交计算</small></footer>
    </>}</Show>
  </div></main>
}
