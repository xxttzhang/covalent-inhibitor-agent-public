import { legacyResult, parseResult, type DiscoveryResult } from "@synsci/util/discovery-result"
import type { DiscoveryProject } from "@synsci/util/discovery-config"
import { createMemo, ErrorBoundary, For, lazy, onCleanup, onMount, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import BackToChat from "./BackToChat"
import { useSDK } from "@/context/sdk"
import { candidateStructures } from "@/artifacts/candidate-structure"
import { format, stale, time } from "./model"
import { refinement, type Refinement, type RefinementRoute } from "./refinement-model"
import PublishedMD from "./PublishedMD"
import RouteNavigation from "./RouteNavigation"
import type { JSX } from "solid-js"
import "./workbench.css"

const Chem2D = lazy(() => import("@/science/renderers/molecular/Chem2D"))
const Structure = lazy(() => import("@/artifacts/CandidateStructurePreview"))

export default function RefinementWorkbench(props: { route: RefinementRoute; config: DiscoveryProject; children?: JSX.Element }) {
  const sdk = useSDK()
  const title = () => props.config.routes[props.route]
  const [state, setState] = createStore({ data: undefined as Refinement | undefined, result: undefined as DiscoveryResult | undefined, error: "", busy: false, now: Date.now(),
    selected: "", stage: "", tab: "structure", all: props.route === "ligand-first", method: "covdock" as "af3" | "covdock" })
  const sync = async () => {
    if (state.busy) return
    setState("busy", true)
    try {
      const response = await sdk.request("/file/content", { cache: "no-store", signal: AbortSignal.timeout(12000) }, { path: title().dataPath! })
      if (!response.ok) throw Error(`任务记录暂不可用（${response.status}）`)
      const envelope = await response.json()
      const packet = parseResult(JSON.parse(envelope.content), { project: props.config.project, target: props.config.target!.id, route: props.route })
      const parsed = refinement.safeParse(legacyResult(packet, { project: props.config.project, target: props.config.target!.id, route: props.route }))
      if (!parsed.success) throw Error("本次状态读取未通过校验，保留上次结果。")
      const data = parsed.data
      if (!state.data && packet.current === "md" && packet.candidates.some(candidate => candidate.md.state !== "pending")) setState({ all: true, tab: "md" })
      if (data.project !== sdk.projectID || data.route !== props.route) throw Error("此项目尚未登记当前路线工作台。")
      if (state.data && JSON.stringify(state.data.sources) === JSON.stringify(data.sources)) setState("data", "collectedAt", data.collectedAt)
      else setState("data", data)
      // Status, replica counts and verified metrics can change while the pinned
      // source list stays identical, so the contract packet must always refresh.
      setState("result", packet)
      if (!data.candidates.some(c => c.row.compound_id === state.selected)) setState("selected", (data.candidates.find(c => c.featured) ?? data.candidates[0]).row.compound_id)
      if (!state.stage) setState("stage", data.current)
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
  const offline = createMemo(() => !!state.error || !!state.data && stale(state.data.collectedAt, state.now))
  const candidates = createMemo(() => state.data?.candidates ?? [])
  const featured = createMemo(() => candidates().filter(c => c.featured))
  const cards = createMemo(() => state.all ? candidates() : featured())
  const selected = createMemo(() => candidates().find(c => c.row.compound_id === state.selected))
  const md = createMemo(() => state.result?.candidates.find(candidate => candidate.id === state.selected)?.md)
  const phase = createMemo(() => state.data?.stages.find(s => s.id === state.stage))
  const refs = createMemo(() => selected() ? candidateStructures(selected()!.row).filter(r => r.method !== "generated") : [])
  const method = createMemo(() => refs().some(ref => ref.method === state.method) ? state.method : refs()[0]?.method)
  const reportUrl = (ref: Refinement["reports"][number]) => sdk.request.url(`/file/artifact-store/${ref.id}/raw`, { versionID: ref.version })
  return <main class="discovery" data-discovery-workbench data-discovery-route={props.route}><div class="discovery-inner">
    <header class="discovery-top"><BackToChat project={sdk.projectID} session={state.data?.session} /><span>共价药物发现 <span class="discovery-separator">/</span> 路线工作台</span><span class="discovery-top-tag">{props.config.target?.label ?? "靶点记录读取中"}</span></header>
    <RouteNavigation project={sdk.projectID} active={props.route} />
    {props.children}
    <section class="discovery-hero"><div><span class="discovery-eyebrow">STRUCTURE-BASED DISCOVERY</span><h1>{title().title}<span>{title().name}</span></h1><p>{title().subtitle}</p></div>
      <div class="discovery-sync" aria-live="polite"><span classList={{ "is-offline": offline() }}><i />{!state.data ? "连接任务记录…" : offline() ? "同步中断 · 保留已验收结果" : "任务记录自动同步"}</span><small>{state.data ? `读取于 ${time(state.data.collectedAt)} · 每 15 秒` : "正在读取真实任务与结果"}</small><button onClick={() => void sync()} disabled={state.busy} aria-label="刷新进度">{state.busy ? "读取中…" : "↻ 刷新进度"}</button></div></section>
    <Show when={state.error}><div class="discovery-alert" role="alert">{state.error} {state.data ? "当前显示最近一次有效数据。" : "暂无可验证的状态数据。"}</div></Show>
    <Show when={state.data} fallback={<div class="discovery-loading">{state.error ? "等待有效记录后可重试" : "加载路线、候选结构与计算证据…"}</div>}>{data => <>
      <section class="discovery-stats" aria-label="本轮计算概况"><For each={data().stats}>{stat => <div><span>{stat.label}</span><strong>{stat.value}<small> {stat.unit}</small></strong><p>{stat.note}</p></div>}</For></section>
      <section class="discovery-progress"><div class="discovery-section-title"><h2>研究进程</h2><span>点击阶段查看详情 · 完成计算 ≠ 验证活性</span></div>
        <div class="discovery-steps" role="group" aria-label="路线阶段"><For each={data().stages}>{(step, i) => <button class="discovery-step" classList={{ "is-done": step.state === "done", "is-current": step.state === "current", "is-blocked": step.state === "blocked", "is-selected": state.stage === step.id }} aria-pressed={state.stage === step.id} onClick={() => setState("stage", step.id)}>
          <span class="discovery-step-number">{step.state === "done" ? "✓" : step.state === "blocked" ? "!" : String(i() + 1).padStart(2, "0")}</span><b>{step.label}</b><small>{step.count}</small><em>{step.status}</em></button>}</For></div>
        <Show when={phase()}>{current => <div class="discovery-phase" classList={{ "has-blocker": current().state === "blocked" }}><div class="discovery-phase-marker" /><div><h3>{current().title}</h3><p>{current().detail}</p><div class="discovery-phase-facts"><For each={current().facts}>{fact => <span>{fact}</span>}</For></div></div></div>}</Show>
      </section>
      <section class="discovery-candidates"><div class="discovery-section-title"><div><h2>候选分子 <span>{cards().length}</span></h2><p>反应前分子结构 · 原始顺序 · 非活性排名</p></div><div class="discovery-segment" role="group" aria-label="候选范围">
        <button aria-pressed={!state.all} onClick={() => { setState("all", false); if (!selected()?.featured && featured().length) setState("selected", featured()[0].row.compound_id) }}>{title().filter} · {featured().length}</button>
        <button aria-pressed={state.all} onClick={() => setState("all", true)}>全部候选 · {candidates().length}</button></div></div>
        <div class="discovery-cards"><For each={cards()}>{candidate => <article class="discovery-card" classList={{ "is-selected": state.selected === candidate.row.compound_id }} data-discovery-candidate={candidate.row.compound_id}>
          <button class="discovery-card-select" aria-label={`查看 ${candidate.row.compound_id}`} aria-pressed={state.selected === candidate.row.compound_id} onClick={() => setState("selected", candidate.row.compound_id)}>
            <span><small>{title().name.toUpperCase()}</small><strong>{candidate.label}</strong></span><span class={`discovery-badge ${candidate.tone}`}>{candidate.badge}</span></button>
          <div class="discovery-molecule"><ErrorBoundary fallback={<p>二维结构暂不可用，详见原始 SMILES。</p>}><Suspense fallback={<p>绘制分子结构…</p>}><Chem2D kind="chem-2d" data={{ smiles: candidate.row.standardized_smiles }} height={175} /></Suspense></ErrorBoundary></div>
          <div class="discovery-card-values"><div><span>CovDock 评分</span><b>{format(candidate.row.covdock_score)}</b></div><div><span>AF3 mPAE</span><b>{format(candidate.row.af3_mpae)}</b></div><span class="discovery-card-arrow" aria-hidden="true">↗</span></div>
        </article>}</For></div>
        <p class="refinement-notice">{data().notice}</p>
      </section>
      <Show when={selected()}>{candidate => <section class="discovery-detail"><div class="discovery-detail-header"><div><span class="discovery-eyebrow">CANDIDATE PROFILE</span><h2>{candidate().label} <span class={`discovery-badge ${candidate().tone}`}>{candidate().badge}</span></h2><p class="refinement-identity">{candidate().row.compound_id}</p></div>
        <div class="discovery-tabs" role="group" aria-label="候选证据"><For each={[["structure", "结合构象"], ["evidence", "计算证据"], ["md", "MD 证据"], ["records", "研究记录"]]}>{item => <button aria-pressed={state.tab === item[0]} onClick={() => setState("tab", item[0])}>{item[1]}</button>}</For></div></div>
        <Show when={state.tab === "structure"}><div class="discovery-structure-grid"><div class="discovery-viewer"><div class="discovery-viewer-heading"><h3>三维结合口袋</h3><div class="discovery-segment"><For each={(["covdock", "af3"] as const)}>{source => <button aria-pressed={method() === source} disabled={!refs().some(ref => ref.method === source)} onClick={() => setState("method", source)}>{source === "covdock" ? "共价对接" : "AF3 预测"}</button>}</For></div></div>
          <Show when={method()} fallback={<p class="discovery-empty">此候选暂无已登记的三维结构。</p>}>{source => <ErrorBoundary fallback={(error, reset) => <div role="alert">结构加载失败：{String(error)} <button onClick={reset}>重试</button></div>}><Suspense fallback={<div class="discovery-loading">校验结构并加载三维口袋…</div>}><Structure row={candidate().row} method={source()} compact /></Suspense></ErrorBoundary>}</Show>
        </div><aside class="discovery-evidence"><h3>当前判断</h3><p class="discovery-recommendation">{candidate().note}</p><dl>
          <div><dt>分子量</dt><dd>{format(candidate().row.mw, 1)} <small>Da</small></dd></div><div><dt>cLogP</dt><dd>{format(candidate().row.clogp)}</dd></div>
          <div><dt>局部应变</dt><dd>{format(candidate().row.local_strain_energy)} <small>kcal/mol</small></dd></div><div><dt>构象比较 RMSD</dt><dd>{format(candidate().row.af3_covdock_ligand_rmsd_angstrom)} <small>Å</small></dd></div>
          <div><dt>原始门控等级</dt><dd>{candidate().row.covdock_gate_detail || "尚未评价"}</dd></div></dl>
          <div class="discovery-evidence-note"><b>{title().evidenceHeading}</b><p>{title().evidenceNote}</p></div>
          <details><summary>原始 SMILES 与身份</summary><code>{candidate().row.standardized_smiles}</code><p>输入索引：{candidate().row.input_sdf_index}</p><p>原始门控理由：{candidate().row.covdock_gate_reason || "待结果完整后评价"}</p></details>
        </aside></div></Show>
        <Show when={state.tab === "evidence"}><div class="discovery-records"><h3>同一候选的独立证据</h3><p>{candidate().note}</p><div class="refinement-evidence-grid">
          <article><span>AF3 预测</span><strong>{format(candidate().row.af3_mpae)} <small>mPAE</small></strong><p>{props.route === "ligand-first" ? "本轮实际样本：1。局部加合物几何异常仍需审阅。" : "已登记原始结构，置信度不等于实验活性。"}</p></article>
          <article><span>共价对接</span><strong>{format(candidate().row.covdock_score)}</strong><p>{candidate().row.covdock_score ? `原始姿势索引 ${candidate().row.covdock_structure_pose} · 厂商评分` : "尚无已完成的对接结果；缺失值未填成零。"}</p></article>
          <article><span>跨方法构象比较</span><strong>{format(candidate().row.af3_covdock_ligand_rmsd_angstrom)} <small>Å</small></strong><p>口袋蛋白对齐后的配体 RMSD；不是 MD 轨迹 RMSD。</p></article>
        </div><Show when={props.route === "ligand-first"}><div class="refinement-notice"><b>AF3 局部几何证据</b><p>SG–C1–C2–C3 二面角：{format(candidate().row.af3_geometry_dihedral)}°。需结合成键拓扑审阅，不能仅以 mPAE 或整体 RMSD 判定结构可靠。</p></div></Show>
          <h3>本轮候选比较</h3><div class="refinement-table-wrap"><table class="refinement-table"><thead><tr><th>候选</th><th>AF3 mPAE</th><th>CovDock</th><th>构象 RMSD / Å</th><th>状态</th></tr></thead><tbody><For each={candidates()}>{entry => <tr classList={{ selected: entry.row.compound_id === state.selected }}><td><button onClick={() => setState("selected", entry.row.compound_id)}>{entry.label}</button></td><td>{format(entry.row.af3_mpae)}</td><td>{format(entry.row.covdock_score)}</td><td>{format(entry.row.af3_covdock_ligand_rmsd_angstrom)}</td><td>{entry.badge}</td></tr>}</For></tbody></table></div>
        </div></Show>
        <Show when={state.tab === "md"}><Show when={md()?.state === "ready" || (md()?.metrics.length ?? 0) > 0} fallback={<div class="discovery-md"><div class="refinement-md-empty"><span class="discovery-eyebrow">MOLECULAR DYNAMICS</span><h3>{md()?.state === "failed" ? "本候选 MD 需复核" : md()?.state === "running" ? "本候选 MD 进行中" : "等待本候选的轨迹证据"}</h3><p>{md()?.summary || data().mdReason}</p></div>
          <div class="refinement-notice">已校验的独立重复：{md()?.independentReplicas ?? 0}。生产完成和分析验收分别记录；尚未验收的结果不作为完整轨迹分析展示。</div>
        </div>}><PublishedMD result={state.result!} candidate={state.selected} /></Show></Show>
        <Show when={state.tab === "records"}><div class="discovery-records"><h3>结果与研究记录</h3><div class="discovery-downloads"><For each={data().reports}>{report => <a href={reportUrl(report)} target="_blank" rel="noreferrer"><span>↗</span><div><b>{report.title}</b><small>{report.name}</small></div></a>}</For></div>
          <details><summary>证据来源与校验值 · {data().sources.length} 份记录</summary><For each={data().sources}>{source => <div class="discovery-source"><b>{source.path}</b><code>{source.sha256}</code></div>}</For></details>
        </div></Show>
      </section>}</Show>
      <footer class="discovery-footer"><span><i /> 科研证据可追溯</span><p>自动读取本路线已同步的本地记录；不代表远端调度器实时心跳。证据更新于 {time(data().evidenceUpdatedAt)}（北京时间）。</p><small>页面查看不提交计算</small></footer>
    </>}</Show>
  </div></main>
}
