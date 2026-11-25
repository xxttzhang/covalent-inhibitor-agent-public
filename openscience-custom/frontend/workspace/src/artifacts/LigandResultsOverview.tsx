import { createMemo, ErrorBoundary, For, lazy, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import { candidateStructures } from "./candidate-structure"
import { consensusLabel, gateDescription } from './candidate-gate'
import { candidateStageSummary, isScientificPareto, paretoPolicyLabel } from './candidate-table'
import "./ligand-results.css"

const Chem2D = lazy(() => import("@/science/renderers/molecular/Chem2D"))
const Structure = lazy(() => import("./CandidateStructurePreview"))
const value = (raw?: string) => raw?.trim() && Number.isFinite(Number(raw)) ? Number(raw).toFixed(2) : "未评价"

export default function LigandResultsOverview(props: { rows: Record<string, string>[]; smiles: string; onOpenArtifact?: (id: string) => void }) {
  const library = createMemo(() => props.rows.every((row) => row.route === 'library-first' || row.result_scope === "library_first_refinement_display"))
  const title = (candidate: Record<string, string>) => library() ? `候选 ${candidate.input_sdf_index}` : candidate.compound_id.split("_").at(-1)
  const [state, setState] = createStore<{ selected: number; method: "af3" | "covdock"; expanded: boolean }>({ selected: 0, method: "covdock", expanded: false })
  const row = createMemo(() => props.rows[state.selected] ?? props.rows[0])
  const refs = createMemo(() => candidateStructures(row()))
  const method = createMemo(() => refs().some((ref) => ref.method === state.method) ? state.method : refs()[0]?.method)
  const ranked = createMemo(() => isScientificPareto(props.rows))
  const stages = createMemo(() => candidateStageSummary(props.rows))
  return <section class="ligand-overview" classList={{ "is-expanded": state.expanded }} data-ligand-overview>
    <header class="ligand-heading"><div><span class="ligand-eyebrow">{library() ? "LIBRARY-FIRST" : "LIGAND-FIRST"} · RESULTS</span><h2>{library() ? "化合物库筛选结果总览" : "共价化候选总览"}</h2><p>{props.rows.length} 个候选 · 点击卡片直接查看结合口袋。{ranked() ? '科研 Pareto 优先级；缺测 / 淘汰者保留但不排名' : '原始顺序 · 未重新排名'}</p></div>
      <button type="button" class="ligand-button" onClick={() => setState("expanded", !state.expanded)}>{state.expanded ? "退出大图" : "展开结果总览"}</button>
    </header>
    <section aria-label="Ligand-first 阶段进度" class="ligand-evidence">
      <span class="ligand-eyebrow">任务阶段</span>
      <div style={{ overflow: "auto" }}><table style={{ width: "100%", "text-align": "left" }}>
        <thead><tr><th>阶段</th><th>状态</th><th>具备成功／警告证据</th></tr></thead>
        <tbody><For each={stages()}>{(stage) => <tr><td>{stage.label}</td><td>{stage.state}</td><td>{stage.completed} / {stage.total}</td></tr>}</For></tbody>
      </table></div>
      <div style={{ display: "flex", gap: "8px", "flex-wrap": "wrap" }}>
        <Show when={props.rows[0]?.selection_report_artifact_id}><button type="button" class="ligand-button" disabled={!props.onOpenArtifact}
          onClick={() => props.onOpenArtifact?.(props.rows[0].selection_report_artifact_id)}>查看 Pareto 选择报告</button></Show>
        <Show when={props.rows[0]?.traceability_report_artifact_id}><button type="button" class="ligand-button" disabled={!props.onOpenArtifact}
          onClick={() => props.onOpenArtifact?.(props.rows[0].traceability_report_artifact_id)}>查看完整溯源报告</button></Show>
      </div>
    </section>
    <div class="ligand-cards">
      <For each={props.rows}>{(candidate, index) => <article class="ligand-card" classList={{ "is-selected": index() === state.selected }} data-ligand-card>
        <button type="button" class="ligand-card-select" aria-pressed={index() === state.selected} aria-label={`查看候选 ${candidate.compound_id}`} onClick={() => setState({ selected: index(), method: "covdock" })}>
          <span class="ligand-card-top"><strong>{title(candidate)}</strong><span class="ligand-gate" classList={{ "is-warning": candidate.covdock_gate_detail !== "A" }}>Gate {candidate.covdock_gate_detail || "未评价"}</span></span>
          <span class="ligand-identity">输入索引 {candidate.input_sdf_index} · {library() ? "库筛选精筛结果" : candidate.result_scope === 'candidate-results-v1' ? '本任务候选' : candidate.result_scope === "historical_two_candidate_reuse" ? "历史结果" : "新增单候选"}</span>
          <Show when={ranked()}><span class="ligand-identity">{candidate.final_rank ? `优先级 ${candidate.final_rank} · Pareto 层 ${candidate.pareto_front}` : `不排名：${candidate.pareto_exclusion_reason || '证据不足'}`}</span></Show>
          <Show when={library()}><span class="ligand-identity" style={{ "overflow-wrap": "anywhere" }}>{candidate.compound_id}</span></Show>
          <span class="ligand-card-action">{index() === state.selected ? "正在查看 ↓" : "查看结合口袋 →"}</span>
        </button>
        <div class="ligand-depiction"><ErrorBoundary fallback={<p>二维图不可用；原始记录保留</p>}><Suspense fallback={<p>加载分子结构…</p>}><Chem2D kind="chem-2d" data={{ smiles: candidate[props.smiles] }} height={185} /></Suspense></ErrorBoundary></div>
        <dl class="ligand-metrics"><div><dt>AF3 mPAE</dt><dd>{value(candidate.af3_mpae)}</dd></div><div><dt>CovDock 评分</dt><dd>{value(candidate.covdock_score)}</dd></div></dl>
      </article>}</For>
    </div>
    <div class="ligand-detail">
      <aside class="ligand-evidence"><span class="ligand-eyebrow">当前候选</span><h3>{title(row())}</h3><p data-candidate-identity>{row().compound_id}</p>
        <dl><div><dt>物理质检</dt><dd>{row().physical_validity || "未评价"}</dd></div><div><dt>独立评价</dt><dd>{row().independent_gate || "未评价"}</dd></div><div><dt>母体核心保留</dt><dd>{value(row().independent_parent_mcs_fraction)}</dd></div><div><dt>跨方法一致性</dt><dd>{consensusLabel(row().cross_method_consensus)}</dd></div>
          <div><dt>构象 RMSD</dt><dd>{value(row().af3_covdock_ligand_rmsd_angstrom)}{row().af3_covdock_ligand_rmsd_angstrom ? " Å" : ""}</dd></div><div><dt>局部应变（原值）</dt><dd>{value(row().local_strain_energy)}</dd></div></dl>
        <p class="ligand-notice">{gateDescription(row().covdock_gate_detail, row().covdock_gate_reason)}</p>
        <Show when={ranked()}><details open><summary>排名依据</summary><p>{paretoPolicyLabel(row().ranking_policy)}</p><p>{row().selection_reason || (row().final_rank ? `Pareto 层 ${row().pareto_front}；最终优先级 ${row().final_rank}` : row().pareto_exclusion_reason || '证据不完整，未纳入排名')}</p></details></Show>
        <details><summary>原始门控理由 / SMILES</summary><p>{row().covdock_gate_reason}</p><p>{row()[props.smiles]}</p></details>
        <p class="ligand-footnote">分数按原值展示，卡片保留两位小数。构象质检不证明实验活性；完整精度见下方原始表。</p>
      </aside>
      <div class="ligand-viewer"><div class="ligand-methods" role="group" aria-label="结构来源">
        <For each={(["covdock", "af3"] as const)}>{(source) => <button type="button" class="ligand-button" aria-pressed={method() === source} disabled={!refs().some((ref) => ref.method === source)} onClick={() => setState("method", source)}>{source === "covdock" ? "CovDock-LO 对接构象" : "AF3 预测构象"}</button>}</For>
      </div><p class="ligand-footnote">两种结构分别查看，非对齐叠合；未登记的来源不可选。默认聚焦唯一配体口袋。</p>
        <Show when={method()} fallback={<p>此候选没有登记三维结构。</p>}>{(source) => <ErrorBoundary fallback={(error, reset) => <p role="alert">{String(error)} <button onClick={reset}>重试</button></p>}><Suspense fallback={<p>正在加载三维查看器…</p>}><Structure row={row()} method={source()} compact /></Suspense></ErrorBoundary>}</Show>
      </div>
    </div>
  </section>
}
