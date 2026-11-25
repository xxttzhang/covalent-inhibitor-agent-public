import { For, Show, onMount, onCleanup, lazy, Suspense, ErrorBoundary } from "solid-js"
import { createStore } from "solid-js/store"
import { useSDK } from "@/context/sdk"
import { studyRequest, studyRoutes, type DiscoveryStudy, type StudyOverview } from "@synsci/util/discovery-study"
import type { DiscoveryProject } from "@synsci/util/discovery-config"
import BackToChat from "./BackToChat"
import RouteNavigation from "./RouteNavigation"
import "./workbench.css"
import { studyStructure } from "./study-structure"
import { studyReport, hypothesisBlockers } from "@synsci/util/discovery-study-report"
import { HypothesisCards } from "./HypothesisCards"
import WorkflowProgress, { useWorkflowObservations } from "./WorkflowProgress"
const Preview = lazy(() => import("@/artifacts/CandidateStructurePreview"))

type Overview = StudyOverview
const labels: Record<string, string> = { title: "研究名称", gene: "靶点基因", target: "靶点标识（须与历史来源一致）", taxon: "物种 Taxonomy ID",
  uniprot: "UniProt", chain: "链", residue: "残基名称", number: "残基编号", atom: "反应原子", receptor: "受体结构路径或标识", rationale: "研究假设与依据" }
const status: Record<string, string> = { "not-selected": "未启用", "awaiting-import": "待导入历史结果", "missing-inputs": "缺少输入",
  "unsupported": "当前执行适配器不支持",
  "awaiting-adapter-validation": "待执行适配器校验", "historical-imported": "历史结果已接入",
  "plan-compiled": "计划已编译，未提交", "local-plan-compiled": "本地计划已编译，未提交", "upstream-plan-bound": "上游计划已接入，未提交" }

export default function StudyWorkbench() {
  const sdk = useSDK()
  const progress = useWorkflowObservations()
  const [state, setState] = createStore({ busy: false, error: "", preview: undefined as ReturnType<typeof studyStructure> | undefined, studies: [] as DiscoveryStudy[], overview: undefined as Overview | undefined,
    fields: { title: "", gene: "", target: "", taxon: "", uniprot: "", chain: "", residue: "", number: "", atom: "", receptor: "", rationale: "" },
    numbering: "canonical" as "canonical" | "structure",
    registeredSource: null as { target: DiscoveryProject["target"]; routes: string[] } | null,
    routes: Object.fromEntries(studyRoutes.map(route => [route, { selected: false, mode: "reuse" as "reuse" | "new", library: "", parent: "", parentStructure: "", receptorStructure: "", pocket: "", warhead: "", parentBinding: "unknown", screeningMode: "", maxPoints: "", slots: "", seed: "" }])) })
  const call = async (body: unknown) => {
    const response = await sdk.request("/api/discovery/study", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    if (!response.headers.get("content-type")?.includes("application/json")) throw Error("统一研究接口尚未发布到当前服务")
    const data = await response.json()
    if (!response.ok) throw Error(data.error || `研究操作失败 (${response.status})`)
    return data
  }
  const perform = async (work: () => Promise<void>) => {
    if (state.busy) return
    setState({ busy: true, error: "" })
    try { await work() } catch (error) { setState("error", String(error)) } finally { setState("busy", false) }
  }
  const list = async () => {
    const result = await call({ action: "list" })
    setState({ studies: result.studies, registeredSource: result.registeredSource ?? null })
  }
  const load = async (id: string) => {
    setState("preview", undefined)
    await call({ action: "sync-hypotheses", study_id: id })
    setState("overview", await call({ action: "get", study_id: id }))
  }
  onMount(() => {
    void perform(list)
    const timer = setInterval(() => {
      const view = state.overview
      if (!view?.reasoning || state.busy || document.hidden) return
      const pending = studyRoutes.some(route => view.plan.routes[route].selected &&
        !view.hypothesisAttachments?.some(item => item.value.hypothesis.route === route))
      if (!pending) return
      void perform(async () => {
        await call({ action: "sync-hypotheses", study_id: view.plan.id })
        const latest = await call({ action: "get", study_id: view.plan.id })
        if (state.overview?.plan.id === view.plan.id) setState("overview", latest)
      })
    }, 15000)
    onCleanup(() => clearInterval(timer))
  })
  const create = async () => {
    const f = state.fields
    const request = studyRequest.parse({ title: f.title, target: { id: f.target, gene: f.gene, taxon: Number(f.taxon), uniprot: f.uniprot,
      site: { chain: f.chain, residue: f.residue, number: Number(f.number), atom: f.atom, numbering: state.numbering }, receptor: { reference: f.receptor } },
      hypothesis: { state: f.rationale.trim() ? "provided" : "pending", rationale: f.rationale, evidence: [] },
      routes: Object.fromEntries(studyRoutes.map(route => {
        const value = state.routes[route]
        return [route, { selected: value.selected, mode: value.mode,
          ...(route === "de-novo" && (value.slots !== "" || value.seed !== "")
            ? { generation: { slots: value.slots === "" ? undefined : Number(value.slots), seed: value.seed === "" ? undefined : Number(value.seed) } } : {}),
          ...(route === "library-first" && (value.screeningMode || value.maxPoints !== "")
            ? { planning: { screeningMode: value.screeningMode, maxPoints: value.maxPoints === "" ? undefined : Number(value.maxPoints) } } : {}),
          inputs: Object.fromEntries(
          Object.entries({ library: value.library, parent: value.parent, pocket: value.pocket, warhead: value.warhead,
            parentStructure: value.parentStructure, receptorStructure: value.receptorStructure,
            parentBinding: route === "ligand-first" ? value.parentBinding : "" }).filter(([, value]) => value !== "")) }]
      })) })
    const created = await call({ action: "create", request })
    await list()
    await load(created.plan.id)
  }
  const report = (readable = false) => {
    if (!state.overview) return
    const url = URL.createObjectURL(new Blob([readable ? studyReport(state.overview) : JSON.stringify(state.overview, null, 2)], { type: readable ? "text/plain;charset=utf-8" : "application/json" }))
    const link = document.createElement("a")
    link.href = url; link.download = `${state.overview.plan.id}.${readable ? "txt" : "json"}`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 10000)
  }
  return <main class="discovery" data-study-workbench><div class="discovery-inner">
    <header class="discovery-top"><BackToChat project={sdk.projectID} /><span>统一研究计划与候选池</span></header>
    <RouteNavigation project={sdk.projectID} active="studies" />
    <WorkflowProgress progress={progress} />
    <h1>统一研究</h1><p>历史结果复用不代表本次完成新计算；新任务输入需经实际路线适配器校验。</p>
    <Show when={state.error}><p role="alert">{state.error}</p></Show>
    <details><summary>创建独立研究计划</summary><form onSubmit={event => { event.preventDefault(); void perform(create) }}>
      <Show when={state.registeredSource?.target}>{target => <p>本项目已登记历史来源：{target().label}；靶点标识：<code>{target().id}</code>；路线：{state.registeredSource?.routes.join("、")}。仅供明确选择历史复用，不自动填写或替换新研究靶点；登记不代表新计算已适配。</p>}</Show>
      <div style={{ display: "grid", "grid-template-columns": "repeat(auto-fit, minmax(240px, 1fr))", gap: "12px" }}>
        <For each={Object.keys(state.fields) as (keyof typeof state.fields)[]}>{key => <label>{labels[key]}<input aria-label={labels[key]} required={key !== "rationale"} value={state.fields[key]} onInput={event => setState("fields", key, event.currentTarget.value)} style={{ display: "block", width: "100%", padding: "8px", border: "1px solid #52617b" }} /></label>}</For>
        <label>位点编号体系<select value={state.numbering} onChange={event => setState("numbering", event.currentTarget.value as typeof state.numbering)}><option value="canonical">标准序列编号</option><option value="structure">结构编号</option></select></label>
      </div>
      <For each={studyRoutes}>{route => <fieldset style={{ padding: "12px", border: "1px solid #52617b", margin: "12px 0" }}><legend>{route}</legend>
        <label><input type="checkbox" checked={state.routes[route].selected} onChange={event => setState("routes", route, "selected", event.currentTarget.checked)} />启用路线</label>
        <select aria-label={`${route} 模式`} value={state.routes[route].mode} onChange={event => setState("routes", route, "mode", event.currentTarget.value as "reuse" | "new")}><option value="reuse">历史结果复用</option><option value="new">新计算计划（不提交）</option></select>
        <For each={route === "library-first" ? ["library", "warhead"] : route === "ligand-first" ? ["parent", "parentStructure", "receptorStructure", "warhead"] : ["pocket", "warhead"]}>{key => <label style={{ display: "block" }}>{key}<input aria-label={`${route} ${key}`} value={state.routes[route][key as "library"]} onInput={event => setState("routes", route, key as "library", event.currentTarget.value)} /></label>}</For>
        <Show when={route === "ligand-first"}><label>母体结合方式<select value={state.routes[route].parentBinding} onChange={event => setState("routes", route, "parentBinding", event.currentTarget.value)}><option value="unknown">未知／待提供证据</option><option value="noncovalent">非共价</option><option value="covalent">共价</option></select></label></Show>
        <Show when={route === "library-first"}><label>计划筛选规模<select aria-label="Library-first 计划筛选规模" value={state.routes[route].screeningMode} onChange={event => setState("routes", route, "screeningMode", event.currentTarget.value)}>
          <option value="">未指定（不能编译执行计划）</option><option value="economy">经济</option><option value="standard">标准</option><option value="deep">深度</option><option value="test100">100 候选测试</option>
        </select></label><label>计划预算上限（点券，不代表批准）<input aria-label="Library-first 计划预算" type="number" min="0" step="any" value={state.routes[route].maxPoints} onInput={event => setState("routes", route, "maxPoints", event.currentTarget.value)} /></label></Show>
        <Show when={route === "de-novo"}><p>当前执行包仅支持 EGFR／4G5J A:C797／acrylamide；口袋引用：registered:egfr-c797-acrylamide-v1。其他输入会明确阻断，不自动替换。</p>
          <label>生成槽位数<input aria-label="De novo 生成槽位数" type="number" min="1" step="1" value={state.routes[route].slots} onInput={event => setState("routes", route, "slots", event.currentTarget.value)} /></label>
          <label>随机种子<input aria-label="De novo 随机种子" type="number" min="0" step="1" value={state.routes[route].seed} onInput={event => setState("routes", route, "seed", event.currentTarget.value)} /></label>
        </Show>
      </fieldset>}</For><button disabled={state.busy} type="submit">创建研究（不启动计算）</button>
    </form></details>
    <section><h2>研究记录</h2><For each={state.studies}>{plan => <button disabled={state.busy} onClick={() => void perform(() => load(plan.id))} style={{ display: "block", padding: "8px" }}>{plan.title} · {plan.target.gene} · {plan.id}</button>}</For></section>
    <Show when={state.overview}>{view => <>
      <h2>{view().plan.title}</h2><p>{view().plan.id} · {view().plan.target.gene} · {view().plan.target.receptor.reference}</p>
      <p>研究假设：{view().plan.hypothesis.rationale || "待科学推理入口提供"}</p>
      <section><h2>科学假设交接</h2>
        <Show when={view().plan.routes["ligand-first"].selected && view().plan.routes["ligand-first"].mode === "new"}>
          <Show when={view().ligandPlan} fallback={<button disabled={state.busy} onClick={() => void perform(async () => {
            await call({ action: "bind-ligand-plan", study_id: view().plan.id }); await load(view().plan.id)
          })}>校验并接入 Ligand-first 上游编译计划（不提交）</button>}>{compiled => <div>
            <p>Ligand-first 上游计划已接入 · 未批准 · 未提交。此处核对编译快照；远端文件和执行条件仍须运行前复核。</p>
            <code>{compiled().planDigest}</code><details><summary>查看 Ligand-first 实际编译计划</summary><pre>{JSON.stringify(compiled().plan, null, 2)}</pre></details>
          </div>}</Show>
        </Show>
        <Show when={view().plan.routes["de-novo"].selected && view().plan.routes["de-novo"].mode === "new"}>
          <Show when={view().denovoPlan} fallback={<button disabled={state.busy} onClick={() => void perform(async () => {
            await call({ action: "compile-denovo", study_id: view().plan.id }); await load(view().plan.id)
          })}>编译 De novo 计划（仅本地校验，不提交）</button>}>{compiled => <div>
            <p>De novo 本地计划已编译 · 未批准 · 未提交。远端资产和实时资源尚待核验。{compiled().createdAt}</p>
            <code>{compiled().planDigest}</code><details><summary>查看 De novo 实际编译计划</summary><pre>{JSON.stringify(compiled().plan, null, 2)}</pre></details>
          </div>}</Show>
        </Show>
        <Show when={view().plan.routes["library-first"].selected && view().plan.routes["library-first"].mode === "new"}>
          <Show when={view().libraryPlan} fallback={<button disabled={state.busy} onClick={() => void perform(async () => {
            await call({ action: "compile-library", study_id: view().plan.id }); await load(view().plan.id)
          })}>编译 Library-first 计划（读取资源信息，不批准、不提交）</button>}>{compiled => <div>
            <p>Library-first 执行计划已编译 · 未批准 · 未提交。资源快照时间：{compiled().createdAt}；运行前仍需原生审批和资源复核。</p>
            <code>{compiled().planDigest}</code><details><summary>查看实际编译计划</summary><pre>{JSON.stringify(compiled().plan, null, 2)}</pre></details>
          </div>}</Show>
        </Show>
        <Show when={view().reasoning} fallback={<button disabled={state.busy} onClick={() => void perform(async () => {
          await call({ action: "start-reasoning", study_id: view().plan.id }); await load(view().plan.id)
        })}>启动科学推理（只整理证据与假设，不提交计算）</button>}>{reasoning => <p>
          <a href={`/${sdk.projectID}/session/${reasoning().session}`}>查看本研究科学推理会话</a> · 此会话禁止科研计算；会话存在不代表推理已完成。
        </p>}</Show>
        <p>本研究唯一匹配的推理产物自动接入；存在多个版本时需手动选择。推理期间每 15 秒刷新，不清除已打开的结构。历史研究不会自动混入。接入不代表计算已启动。</p>
        <For each={studyRoutes.filter(route => view().plan.routes[route].selected)}>{route => <div><h3>{route}</h3>
        <Show when={view().hypothesisReuses?.find(item => item.route === route)} fallback={<details><summary>显式复用其他研究的已有推理（仅证据）</summary>
          <p>核对靶点、物种和位点；保留原研究和原受体。不复用执行许可或计算计划。</p>
          <For each={view().reusableHypotheses?.filter(item => item.route === route)} fallback={<p>没有身份匹配的历史推理。</p>}>{item => <div>
            <p>{item.title} · 原受体：{item.receptor}</p><code>{item.sha256}</code>
            <button disabled={state.busy} onClick={() => void perform(async () => {
              await call({ action: "reuse-hypothesis", study_id: view().plan.id, source_study: item.sourceStudy, route, sha256: item.sha256 }); await load(view().plan.id)
            })}>显式复用此推理证据</button>
          </div>}</For>
        </details>}>{item => <section>
          <h4>历史推理已显式复用 · 不可用于直接提交计算</h4>
          <p>来源研究：{item().sourcePlan.title} · {item().sourcePlan.id}</p>
          <p>原受体：{item().sourcePlan.target.receptor.reference}；当前计划受体：{view().plan.target.receptor.reference}。结构差异必须另行核验。</p>
          <p>接入时间：{item().importedAt}；来源：{item().source}</p><code>{item().sha256}</code>
          <HypothesisCards original={item().original} />
          <details><summary>查看原始证据（保留原研究 ID）</summary><pre style={{ "white-space": "pre-wrap", "overflow-wrap": "anywhere" }}>{item().original}</pre></details>
        </section>}</Show>
        <Show when={(view().hypothesisAttachments ?? (view().hypothesisAttachment ? [view().hypothesisAttachment!] : [])).find(item => item.value.hypothesis.route === route)} fallback={<>
          <Show when={!view().hypothesisSources?.some(item => item.route === route)}><p>{view().hypothesisReuses?.some(item => item.route === route) ? "本次新生成假设尚未接入；上方仅为历史证据。" : "未接入：尚无匹配假设。请通过科学推理入口生成带有本研究 ID 的假设；不会自动借用其他研究。"}</p></Show>
          <For each={view().hypothesisSources?.filter(item => item.route === route)}>{proposal => <div><p>{proposal.route} · {proposal.state}</p><code>{proposal.source}</code>
            <button disabled={state.busy} onClick={() => void perform(async () => {
              await call({ action: "attach-hypothesis", study_id: view().plan.id, source: proposal.source }); await load(view().plan.id)
            })}>接入此科学假设</button></div>}</For>
        </>}>{attachment => <>
          <HypothesisCards original={attachment().original} />
          <p>原始交接状态：{attachment().value.hypothesis.route} · {attachment().value.handoff.state}</p>
          <For each={hypothesisBlockers(attachment().value.handoff)}>{reason => <p>{reason}</p>}</For>
          <p>执行绑定：{attachment().executionBinding}</p><code>{attachment().sha256}</code>
          <Show when={attachment().contractValidation}>{validation => <div><p>实际执行接口校验：{validation().state}（不是计划编译或提交）</p>
            <For each={validation().blockers}>{reason => <p>{reason}</p>}</For></div>}</Show>
          <details><summary>查看原始证据</summary><pre style={{ "white-space": "pre-wrap", "overflow-wrap": "anywhere" }}>{attachment().original}</pre></details>
        </>}</Show></div>}</For>
        <button disabled={state.busy} onClick={() => void perform(() => load(view().plan.id))}>刷新假设与研究状态</button>
      </section>
      <For each={view().routes}>{route => <section style={{ border: "1px solid #52617b", padding: "12px", margin: "12px 0" }}><h3>{route.route} · {status[route.state] ?? route.state}</h3><p>{route.reason}</p>
        <Show when={route.missing.length}><p>缺少：{route.missing.join("、")}</p></Show>
        <Show when={route.state === "awaiting-import"}><button disabled={state.busy} onClick={() => void perform(async () => { await call({ action: "import", study_id: view().plan.id, route: route.route }); await load(view().plan.id) })}>导入已登记历史结果</button></Show>
        <Show when={route.imported}>{value => <><p>历史运行：{value().receipt.source.run} · 证据时间：{value().receipt.source.evidenceUpdatedAt}</p><code>{value().receipt.source.sha256}</code><For each={value().data.stages}>{stage => <p>{stage.label}：{stage.status}（{stage.state}）</p>}</For></>}</Show>
      </section>}</For>
      <h2>统一候选池 · {view().pool.groups.length} 条身份记录 / {view().pool.sourceCount} 条来源</h2>
      <p>不混排不同方法原始分数。RDKit：{view().pool.rdkitVersion ?? "不可用，未合并"}。未确定身份的记录独立保留。</p>
      <p>身份处理策略：{view().pool.policy}。身份分组由受限化学处理环境生成，不修改原始候选、结构或评分；全部来源仍保留。</p>
      <For each={view().pool.groups}>{group => <details><summary>{group.sources[0].candidate.label} · {group.sources.length} 个来源 · {group.identity.state}</summary><code>{group.identity.canonical ?? group.sources[0].candidate.smiles}</code><p>{group.identity.reason}</p>
        <For each={group.sources}>{source => <section><h3>{source.route} · {source.candidate.id}</h3><p>{source.candidate.note}</p><p>历史运行：{source.sourceRun}</p><For each={source.candidate.metrics}>{metric => <p>{metric.label}：{metric.value === null ? "未提供" : metric.value} {metric.unit} · {metric.definition}</p>}</For><p>已登记结构：{source.candidate.structures.map(ref => ref.method).join("、") || "无"}</p></section>}</For>
      </details>}</For>
      <h2>结构查看</h2><p>选择候选和明确的方法／姿势；复用已有查看器。原始坐标展示不代表对齐叠合或共价反应已验证。</p>
      <For each={view().candidates}>{source => <div><span>{source.route} · {source.candidate.label} </span><For each={source.candidate.structures}>{(ref, index) => <button onClick={() => {
        try { setState("preview", studyStructure(source, index())); setState("error", "") }
        catch (error) { setState("preview", undefined); setState("error", String(error)) }
      }}>{ref.method} · {ref.pose === undefined ? "原始结构" : `姿势 ${ref.pose}`}</button>}</For></div>}</For>
      <Show when={state.preview} keyed>{preview => <ErrorBoundary fallback={error => <p role="alert">结构加载失败：{String(error)}</p>}><Suspense fallback={<p>加载结构查看器…</p>}><Preview row={preview.row} method={preview.method} /></Suspense></ErrorBoundary>}</Show>
      <h2>研究报告</h2><details><summary>阅读可追溯证据报告</summary><pre style={{ "white-space": "pre-wrap", "overflow-wrap": "anywhere" }}>{studyReport(view())}</pre></details>
      <button onClick={() => report(true)}>下载可读报告</button><button onClick={() => report(false)}>下载研究证据 JSON（含来源及结构引用）</button>
    </>}</Show>
  </div></main>
}
