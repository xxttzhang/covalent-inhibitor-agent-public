import { legacyResult } from "@synsci/util/discovery-result"
import { renderTemplate, type DiscoveryProject } from "@synsci/util/discovery-config"
import { createMemo, ErrorBoundary, For, lazy, onCleanup, onMount, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import BackToChat from "./BackToChat"
import { useSDK } from "@/context/sdk"
import { format, recommendation, stages, stale, time, workbench, type Workbench } from "./model"
import "./workbench.css"
import MDAnalysis from "./MDAnalysis"
import RouteNavigation from "./RouteNavigation"
import type { JSX } from "solid-js"

const Chem2D = lazy(() => import("@/science/renderers/molecular/Chem2D"))
const Structure = lazy(() => import("@/artifacts/CandidateStructurePreview"))

export default function DeNovoWorkbench(props: { config: DiscoveryProject; children?: JSX.Element }) {
  const settings = () => props.config.routes["de-novo"]
  const sdk = useSDK()
  const [state, setState] = createStore({ data: undefined as Workbench | undefined, error: "", busy: false,
    now: Date.now(), selected: settings().featured[0] ?? "", stage: settings().defaultStage, tab: "structure", method: "covdock" as "covdock" | "generated", all: false })
  const sync = async () => {
    if (state.busy) return
    setState("busy", true)
    try {
      const response = await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(12000) }, { path: settings().dataPath! })
      if (!response.ok) throw Error(`任务记录暂不可用（${response.status}）`)
      const envelope = await response.json()
      const parsed = workbench.safeParse(legacyResult(JSON.parse(envelope.content), { project: props.config.project, target: props.config.target!.id, route: "de-novo" }))
      if (!parsed.success) throw Error("本次状态读取未通过校验，保留上次结果。")
      if (parsed.data.project !== sdk.projectID) throw Error("此项目尚未登记 De novo 工作台。")
      // Keep molecular viewer mounted on heartbeat-only refreshes.
      if (state.data && JSON.stringify(state.data.sources) === JSON.stringify(parsed.data.sources))
        setState("data", "collectedAt", parsed.data.collectedAt)
      else setState("data", parsed.data)
      if (!parsed.data.candidates.some(c => c.row.compound_id === state.selected)) setState("selected", parsed.data.candidates[0]?.row.compound_id ?? "")
      setState("error", "")
    } catch (error) { setState("error", error instanceof Error ? error.message : "读取失败") }
    finally { setState({ busy: false, now: Date.now() }) }
  }
  onMount(() => {
    void sync()
    const poll = setInterval(() => { if (!document.hidden) void sync() }, 15000)
    const clock = setInterval(() => setState("now", Date.now()), 5000)
    const visible = () => { if (!document.hidden) void sync() }
    document.addEventListener("visibilitychange", visible)
    onCleanup(() => { clearInterval(poll); clearInterval(clock); document.removeEventListener("visibilitychange", visible) })
  })
  const disconnected = createMemo(() => !!state.error || !!state.data && stale(state.data.collectedAt, state.now))
  const candidates = createMemo(() => state.data?.candidates ?? [])
  const cards = createMemo(() => state.all ? candidates() : settings().featured.flatMap(id => candidates().filter(c => c.row.compound_id === id)))
  const selected = createMemo(() => candidates().find(c => c.row.compound_id === state.selected))
  const phase = createMemo(() => state.data && stages(state.data, settings()).find(s => s.id === state.stage))
  const reportUrl = (name: string) => {
    const ref = state.data?.reports.find(r => r.name === name)
    return ref ? sdk.request.url(`/file/artifact-store/${ref.id}/raw`, { versionID: ref.version }) : undefined
  }
  return <main class="discovery" data-discovery-workbench>
    <div class="discovery-inner">
      <header class="discovery-top"><BackToChat project={sdk.projectID} session={state.data?.session} /><span>共价药物发现 <span class="discovery-separator">/</span> 路线工作台</span><span class="discovery-top-tag">{props.config.target?.label}</span></header>
      <RouteNavigation project={sdk.projectID} active="de-novo" />
      {props.children}
      <section class="discovery-hero"><div><span class="discovery-eyebrow">STRUCTURE-BASED DISCOVERY</span><h1>{settings().title}<span>{settings().name}</span></h1><p>{settings().subtitle}</p></div><div class="discovery-sync" aria-live="polite"><span classList={{ "is-offline": disconnected() }}><i />{!state.data ? "连接任务记录…" : disconnected() ? "同步中断 · 保留已验收结果" : "任务记录自动同步"}</span><small>{state.data ? `读取于 ${time(state.data.collectedAt)} · 每 15 秒` : "正在读取真实任务与结果"}</small><button onClick={() => void sync()} disabled={state.busy} aria-label="刷新进度">{state.busy ? "读取中…" : "↻ 刷新进度"}</button></div></section>
      <Show when={state.error}><div class="discovery-alert" role="alert">{state.error} {state.data ? "当前显示最近一次有效数据。" : "确认项目已接入状态采集后可重试。"}</div></Show>
      <Show when={state.data} fallback={<div class="discovery-loading">{state.error ? "暂无可验证的状态数据" : "加载路线、候选结构与计算证据…"}</div>}>{data => <>
        <section class="discovery-stats" aria-label="本轮计算概况"><For each={renderTemplate(settings().stats, { ...data(), featuredCount: settings().featured.length })}>{stat => <div><span>{stat.label}</span><strong>{stat.value}<small> {stat.unit}</small></strong><p>{stat.note}</p></div>}</For></section>
        <section class="discovery-progress"><div class="discovery-section-title"><h2>研究进程</h2><span>点击阶段查看详情 <span class="discovery-separator">·</span> 完成计算 ≠ 验证活性</span></div><div class="discovery-steps" role="group" aria-label="路线阶段"><For each={stages(data(), settings())}>{(step, index) => <button class="discovery-step" classList={{ "is-done": step.state === "done", "is-current": step.state === "current", "is-selected": state.stage === step.id }} aria-pressed={state.stage === step.id} onClick={() => setState("stage", step.id)}><span class="discovery-step-number">{step.state === "done" ? "✓" : String(index() + 1).padStart(2, "0")}</span><b>{step.label}</b><small>{step.count}</small><em>{step.status}</em></button>}</For></div><Show when={phase()}>{current => <div class="discovery-phase"><div class="discovery-phase-marker" /><div><h3>{current().title}</h3><p>{current().detail}</p><div class="discovery-phase-facts"><For each={current().facts}>{fact => <span>{fact}</span>}</For></div></div><Show when={current().id === "amber"}><div class="discovery-phase-side"><b>准备阶段</b><span>尚无生产轨迹</span><small>最近证据 {time(data().evidenceUpdatedAt)}</small></div></Show></div>}</Show></section>
        <section class="discovery-candidates"><div class="discovery-section-title"><div><h2>候选分子 <span>{cards().length}</span></h2><p>展示反应前分子结构 · 建议用于后续研究安排，非活性排名</p></div><div class="discovery-segment" role="group" aria-label="候选范围"><button aria-pressed={!state.all} onClick={() => { setState("all", false); if (!selected()?.md) setState("selected", settings().featured[0] ?? candidates()[0]?.row.compound_id ?? "") }}>{settings().filter} · {settings().featured.length}</button><button aria-pressed={state.all} onClick={() => setState("all", true)}>全部候选 · {candidates().length}</button></div></div><div class="discovery-cards"><For each={cards()}>{candidate => <article class="discovery-card" classList={{ "is-selected": state.selected === candidate.row.compound_id }} data-discovery-candidate={candidate.row.compound_id}><button class="discovery-card-select" aria-label={`查看 ${candidate.row.compound_id}`} aria-pressed={state.selected === candidate.row.compound_id} onClick={() => setState("selected", candidate.row.compound_id)}><span><small>DE NOVO</small><strong>{candidate.row.compound_id.split("_").at(-1)}</strong></span><span class={`discovery-badge ${recommendation(candidate.row.compound_id, settings()).tone}`}>{recommendation(candidate.row.compound_id, settings()).label}</span></button><div class="discovery-molecule"><ErrorBoundary fallback={<p>二维结构暂不可用，详见原始 SMILES。</p>}><Suspense fallback={<p>绘制分子结构…</p>}><Chem2D kind="chem-2d" data={{ smiles: candidate.row.standardized_smiles }} height={175} /></Suspense></ErrorBoundary></div><div class="discovery-card-values"><div><span>CovDock 评分</span><b>{format(candidate.row.covdock_score)}</b></div><div><span>{candidate.md ? "环系 RMSD · Å" : "局部应变 · kcal/mol"}</span><b>{candidate.md ? format(candidate.md.rmsd.mean) : format(candidate.row.local_strain_energy)}<small>{candidate.md ? ` ± ${format(candidate.md.rmsd.sd_between_replicas)}` : ""}</small></b></div><span class="discovery-card-arrow" aria-hidden="true">↗</span></div></article>}</For></div></section>
        <Show when={selected()}>{candidate => <section class="discovery-detail"><div class="discovery-detail-header"><div><span class="discovery-eyebrow">CANDIDATE PROFILE</span><h2>{candidate().row.compound_id} <span class={`discovery-badge ${recommendation(candidate().row.compound_id, settings()).tone}`}>{recommendation(candidate().row.compound_id, settings()).label}</span></h2></div><div class="discovery-tabs" role="group" aria-label="候选证据"><For each={[["structure", "结合构象"], ["md", "MD 证据"], ["records", "研究记录"]]}>{item => <button aria-pressed={state.tab === item[0]} onClick={() => setState("tab", item[0])}>{item[1]}</button>}</For></div></div>
          <Show when={state.tab === "structure"}><div class="discovery-structure-grid"><div class="discovery-viewer"><div class="discovery-viewer-heading"><h3>三维结合口袋</h3><div class="discovery-segment"><button aria-pressed={state.method === "covdock"} onClick={() => setState("method", "covdock")}>共价对接</button><button aria-pressed={state.method === "generated"} onClick={() => setState("method", "generated")}>原始生成</button></div></div><ErrorBoundary fallback={(e, reset) => <div role="alert">结构加载失败：{String(e)} <button onClick={reset}>重试</button></div>}><Suspense fallback={<div class="discovery-loading">校验结构并加载三维口袋…</div>}><Structure row={candidate().row} method={state.method} bond={settings().bond} compact /></Suspense></ErrorBoundary></div><aside class="discovery-evidence"><h3>选择依据</h3><p class="discovery-recommendation">{candidate().md ? recommendation(candidate().row.compound_id, settings()).detail : candidate().row.md_reason}</p><dl><div><dt>分子量</dt><dd>{format(candidate().molecularWeight, 1)} <small>Da</small></dd></div><div><dt>cLogP</dt><dd>{format(candidate().logP)}</dd></div><div><dt>局部应变</dt><dd>{format(candidate().row.local_strain_energy)} <small>kcal/mol</small></dd></div><div><dt>全局应变</dt><dd>{format(candidate().row.solvated_global_strain_kcal_mol)} <small>kcal/mol</small></dd></div><div><dt>对接氢键残基</dt><dd>{candidate().row.hydrogen_bond_residues || "未检出"}</dd></div></dl><div class="discovery-evidence-note"><b>{candidate().md ? "重复 MD 已完成，采样不足" : "本轮未开展 MD"}</b><p>对接姿势来自原始共价加合物。二维图为反应前分子；生成和对接构象分别查看，未叠合。</p></div><details><summary>原始 SMILES 与入选依据</summary><code>{candidate().row.standardized_smiles}</code><p>{candidate().row.md_reason}</p><p>{candidate().row.md_limitation}</p><small>以上入选依据为 MD 前记录；后续研究建议见当前采样评估。</small></details></aside></div></Show>
          <Show when={state.tab === "md"}><div class="discovery-md"><Show when={candidate().md} fallback={<p class="discovery-empty">此候选未进入本轮 MD。请查看其对接结构及未入选依据。</p>}>{md => <><div class="discovery-md-summary"><div><span>环系 RMSD</span><strong>{format(md().rmsd.mean)} <small>± {format(md().rmsd.sd_between_replicas)} Å</small></strong><p>{md().replicas} 次独立重复均值 ± 重复间 SD</p></div><div><span>初始接触保持</span><strong>{md().contacts ? format(md().contacts!.mean * 100, 1) : "—"}<small> %</small></strong><p>{settings().md?.contactNote}</p></div><div><span>采样结论</span><strong class="discovery-md-warning">需追加验证</strong><p>{settings().md?.durationNote}</p></div></div><Show when={settings().md}>{mdSettings => <MDAnalysis candidate={candidate().row.compound_id} settings={mdSettings()} />}</Show><p class="discovery-md-footnote">{settings().md?.footer}</p><a class="discovery-link" href={reportUrl(settings().md?.reportName ?? "")} target="_blank" rel="noreferrer">{settings().md?.reportLabel} ↗</a></>}</Show></div></Show>
          <Show when={state.tab === "records"}><div class="discovery-records"><h3>结果与研究记录</h3><div class="discovery-downloads"><For each={data().reports}>{report => <a href={reportUrl(report.name)} target="_blank" rel="noreferrer"><span>↗</span><div><b>{report.title}</b><small>{report.name}</small></div></a>}</For></div><h3>Amber 准备记录</h3><p>以下为已同步的封端模型探针；不代表完整蛋白体系通过。</p><For each={data().amber.probes}>{probe => <div class="discovery-probe"><b>{probe.system}</b><span>{probe.status === "CAPPED_ADDUCT_AMBER_PROBE_PASSED" ? "封端模型探针通过" : probe.status === "RUNNING" ? "记录为运行中，需核对更新时间" : "探针未通过 · 保留失败记录"}</span><time>{time(probe.updated)}</time></div>}</For><details><summary>证据来源与校验值 · {data().sources.length} 份记录</summary><For each={data().sources}>{source => <div class="discovery-source"><b>{source.path}</b><code>{source.sha256}</code></div>}</For></details></div></Show>
        </section>}</Show>
        <footer class="discovery-footer"><span><i /> 科研证据可追溯</span><p>自动读取已同步的本地任务记录；不代表远端调度器实时心跳。证据更新于 {time(data().evidenceUpdatedAt)}（北京时间）。</p><small>页面查看不提交 MD 计算</small></footer>
      </>}</Show>
    </div>
  </main>
}
