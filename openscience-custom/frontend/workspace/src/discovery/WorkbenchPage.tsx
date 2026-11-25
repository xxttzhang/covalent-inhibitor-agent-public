import { createResource, lazy, onCleanup, onMount, Show, Suspense } from "solid-js"
import { useSDK } from "@/context/sdk"
import { discoveryProject, routeDefaults, type DiscoveryProject } from "@synsci/util/discovery-config"
import BackToChat from "./BackToChat"
import RouteNavigation from "./RouteNavigation"
import WorkflowProgress, { useWorkflowObservations } from "./WorkflowProgress"
import "./workbench.css"

const Standard = lazy(() => import("./StandardWorkbench"))
const DeNovo = lazy(() => import("./DeNovoWorkbench"))
const Refinement = lazy(() => import("./RefinementWorkbench"))
export default function WorkbenchPage(props: { route: keyof DiscoveryProject["routes"] }) {
  const sdk = useSDK()
  const progress = useWorkflowObservations()
  const bound = () => progress.result()?.workflows.some(item => item.route === props.route)
  const [result, { refetch }] = createResource(() => sdk.projectID, async project => {
    try {
    const response = await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(20000) }, { path: ".discovery/project.json" })
    if (response.status === 404) return { config: null, error: "" }
    if (!response.ok) throw Error("项目配置读取失败")
    const envelope = await response.json()
    if (!envelope.content?.trim()) return { config: null, error: "" }
    const parsed = discoveryProject.parse(JSON.parse(envelope.content))
    if (parsed.project !== project) throw Error("项目配置身份不匹配，未读取任何计算结果")
    return { config: parsed, error: "" }
    } catch (error) { return { config: null, error: String(error) } }
  })
  onMount(() => { const timer = setInterval(() => { if (result()?.error && !document.hidden) void refetch() }, 15000); onCleanup(() => clearInterval(timer)) })
  const config = () => result()?.config
  const error = () => result()?.error
  const ready = () => { const value = config(); return value?.target && value.routes[props.route].dataPath ? value : undefined }
  return <Show when={!result.loading} fallback={<main class="discovery"><div class="discovery-loading">读取当前项目配置…</div></main>}>
    <Show when={!error() && ready()} fallback={<main class="discovery" data-discovery-empty><div class="discovery-inner">
      <header class="discovery-top"><BackToChat project={sdk.projectID} /><span>共价药物发现 / 路线工作台</span><span class="discovery-top-tag">{config()?.target?.label ?? (bound() ? "聊天研究已登记" : "靶点待配置")}</span></header>
      <RouteNavigation project={sdk.projectID} active={props.route} />
      <WorkflowProgress progress={progress} route={props.route} />
      <section class="discovery-hero"><div><span class="discovery-eyebrow">STRUCTURE-BASED DISCOVERY</span><h1>{routeDefaults[props.route].title}<span>{routeDefaults[props.route].name}</span></h1><p>{routeDefaults[props.route].subtitle}</p></div></section>
      <section class="discovery-progress"><div class="refinement-md-empty"><h3>{error() ? "配置需要检查" : bound() ? "尚无已登记的候选可视化" : config()?.target ? "待运行" : "待配置"}</h3>
        <p>{error() ? "当前项目的配置未通过校验，请检查项目身份与配置格式后重试。" : bound() ? "聊天研究已登记于上方。候选与 MD 图表须有对应的结果登记后才能展示；没有图表不表示研究尚未开始。" : config()?.target ? "靶点已配置，当前路线尚未登记计算结果。" : "为此项目配置靶点、结构与研究位点后，即可沿用相同的路线工作台。"}</p>
        <button class="discovery-link" onClick={() => void refetch()}>重新读取配置 ↻</button></div></section>
      <section class="discovery-candidates"><div class="discovery-section-title"><h2>候选可视化</h2></div><div class="discovery-empty">尚无已登记的候选可视化；这不是候选数量或科学结果的判定。</div></section>
    </div></main>}>{value => <Suspense fallback={<div class="discovery-loading">加载工作台…</div>}>
      <Show when={value().routes[props.route].view !== "standard"} fallback={<Standard config={value()} route={props.route}><WorkflowProgress progress={progress} route={props.route} /></Standard>}><Show when={props.route === "de-novo"} fallback={<Refinement route={props.route as "library-first" | "ligand-first"} config={value()}><WorkflowProgress progress={progress} route={props.route} /></Refinement>}><DeNovo config={value()}><WorkflowProgress progress={progress} route={props.route} /></DeNovo></Show></Show>
    </Suspense>}</Show>
  </Show>
}
