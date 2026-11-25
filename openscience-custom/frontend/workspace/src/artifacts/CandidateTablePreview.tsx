import { createMemo, ErrorBoundary, For, lazy, Show, Suspense } from "solid-js"
import { createStore } from "solid-js/store"
import { candidatePresentation, candidateTable, isScientificPareto } from "./candidate-table"
import { candidateStructures } from "./candidate-structure"

const Chem2D = lazy(() => import("@/science/renderers/molecular/Chem2D"))
const Structure = lazy(() => import("./CandidateStructurePreview"))
const Overview = lazy(() => import("./LigandResultsOverview"))
const Denovo = lazy(() => import('./DenovoResultsOverview'))
const labels: Record<string, string> = {
  md_priority: 'MD 优先级', md_tier: 'MD 证据分组', md_reason: 'MD 决定依据', md_limitation: '局限',
  generated_scaffold_rmsd_A: '生成／对接骨架 RMSD（Å）', solvated_global_strain_kcal_mol: '溶剂化全局应变（kcal/mol）',
  final_rank: '最终计算优先级', pareto_front: 'Pareto 层', pareto_eligible: '是否具备排名证据',
  pareto_exclusion_reason: '不排名原因', ranking_policy: '排序策略', selection_reason: '排名依据',
  experimental_priority_band: '实验优先级分组', independent_gate: '独立评价门控',
  independent_parent_mcs_fraction: '母体核心保留比例', independent_failure_reasons: '独立评价失败原因',
  independent_warning_reasons: '独立评价警告', online_structural_status: '在线结构质检',
  input_sdf_index: "SDF 索引", compound_id: "化合物 ID", carsiscore: "CarsiScore（原值）",
  rtmscore: "RTMScore（原值）", covdock_score: "CovDock 评分（原值）",
  af3_mpae: "AF3 mPAE（原值）", local_strain_energy: "局部应变（原值）",
  af3_covdock_ligand_rmsd_angstrom: "AF3–CovDock RMSD（Å，原值）", result_scope: "结果来源范围",
  engine_status: "引擎状态", covdock_gate_detail: "门控分级", physical_validity: "物理质检",
  cross_method_consensus: "跨方法一致性", covdock_gate_reason: "原因／缺失证据", failure_reason: "失败原因",
}

export default function CandidateTablePreview(props: { content: string; onOpenArtifact?: (id: string) => void }) {
  const table = createMemo(() => candidateTable(props.content))
  const [state, setState] = createStore<{ selected: number; structure?: "af3" | "covdock" | "generated" }>({ selected: 0 })
  const presentation = createMemo(() => candidatePresentation(table()?.rows ?? []))
  const selected = createMemo(() => table()?.rows[state.selected] ?? table()?.rows[0])
  const ranked = createMemo(() => isScientificPareto(table()?.rows ?? []))
  return <Show when={table()} fallback={<pre style={{ "white-space": "pre-wrap" }}>{props.content}</pre>}>{(data) => <section data-candidate-table>
    <Show when={presentation() === "de-novo"}><Suspense fallback={<p>加载生成与对接比较…</p>}><Denovo rows={data().rows} /></Suspense></Show>
    <Show when={presentation() === "overview"}><ErrorBoundary fallback={<p role="alert">总览暂不可用，请查看下方完整表格。</p>}><Suspense fallback={<p>正在加载候选总览…</p>}><Overview rows={data().rows} smiles={data().smiles} onOpenArtifact={props.onOpenArtifact} /></Suspense></ErrorBoundary></Show>
    <Show when={presentation() === "table"}>
    <p>{data().rows.length} 条记录；{ranked() ? '按照科研 Pareto 优先级展示，缺测 / 淘汰者保留但不排名。' : '保持文件顺序，不重新排名。'}二维图展示表内 SMILES，不代表已对接姿势或实验命中。</p>
    <label>选择候选
      <select aria-label="候选分子" value={state.selected} style={{ width: "100%" }} onChange={(e) => setState({ selected: Number(e.currentTarget.value), structure: undefined })}>
        <For each={data().rows}>{(row, index) => <option value={index()}>{row.input_sdf_index ?? index() + 1} · {row.compound_id}</option>}</For>
      </select>
    </label>
    <Show when={selected()}>{(row) => <>
      <p data-candidate-identity>{row().compound_id}</p>
      <Show when={row()[data().smiles]?.trim()} fallback={<p>此记录缺少 SMILES，保留记录但不绘制分子。</p>}>
        <ErrorBoundary fallback={<p role="alert">二维预览失败；原始记录和下载不受影响。</p>}>
          <Suspense fallback={<p>正在加载 RDKit 二维查看器…</p>}>
            <Chem2D kind="chem-2d" data={{ smiles: row()[data().smiles] }} height={260} />
          </Suspense>
        </ErrorBoundary>
      </Show>
      <p style={{ "overflow-wrap": "anywhere" }}>{data().smiles}：{row()[data().smiles] || "未提供"}</p>
      <Show when={candidateStructures(row()).length} fallback={<p>此候选未登记三维查看入口；二维结构不代表结合构象。</p>}>
        <For each={candidateStructures(row())}>{(ref) => <button type="button" onClick={() => setState("structure", ref.method)}>
          查看{ref.method === "af3" ? " AF3" : " CovDock"}结构／结合口袋
        </button>}</For>
        <Show when={state.structure}>{(method) => <>
          <button type="button" onClick={() => setState("structure", undefined)}>关闭三维查看</button>
          <ErrorBoundary fallback={(error, reset) => <p role="alert">结构预览失败：{String(error)}。原始结果不受影响。<button type="button" onClick={reset}>重试查看</button></p>}>
            <Suspense fallback={<p>正在加载结构查看入口…</p>}><Structure row={row()} method={method()} /></Suspense>
          </ErrorBoundary>
        </>}</Show>
      </Show>
    </>}</Show>
    </Show>
    <details open={presentation() === "table"}><summary>完整评分与原始证据表（保持原值）</summary>
    <div style={{ overflow: "auto", "max-height": "360px" }}>
      <table style={{ "border-collapse": "collapse", "font-size": "12px" }}>
        <thead><tr><For each={Object.keys(labels).filter((key) => data().header.includes(key))}>{(key) => <th style={{ padding: "8px", "text-align": "left" }}>{labels[key]}</th>}</For></tr></thead>
        <tbody><For each={data().rows}>{(row, index) => <tr aria-selected={index() === state.selected}>
          <For each={Object.keys(labels).filter((key) => data().header.includes(key))}>{(key) => <td style={{ padding: "8px", "border-top": "1px solid var(--border-weak-base)" }}>{row[key] || "未提供"}</td>}</For>
        </tr>}</For></tbody>
      </table>
    </div>
    </details>
    <details><summary>查看原始 CSV</summary><pre style={{ "white-space": "pre-wrap", "overflow-wrap": "anywhere" }}>{props.content}</pre></details>
  </section>}</Show>
}
