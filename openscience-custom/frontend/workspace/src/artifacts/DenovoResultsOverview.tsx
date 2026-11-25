import { createMemo, For, lazy, Show, Suspense, ErrorBoundary } from 'solid-js'
import { createStore } from 'solid-js/store'
import './ligand-results.css'
import { useSDK } from '@/context/sdk'

const Structure = lazy(() => import('./CandidateStructurePreview'))
const Chem2D = lazy(() => import('@/science/renderers/molecular/Chem2D'))
const number = (v: string) => v?.trim() && Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '未报告'

export default function DenovoResultsOverview(props: { rows: Record<string, string>[] }) {
  const sdk = useSDK()
  const [state, setState] = createStore({ selected: 0, expanded: false, method: 'covdock' as 'generated' | 'covdock', compare: false })
  const row = createMemo(() => props.rows[state.selected] ?? props.rows[0])
  return <section class="ligand-overview" classList={{ 'is-expanded': state.expanded }} data-denovo-comparison>
    <header class="ligand-heading"><div><span class="ligand-eyebrow">DE NOVO · COVDOCK REVIEW</span><h2>生成与共价对接结果比较</h2><p>{props.rows.length} 个候选 · EGFR C797 · 保留全部候选；MD 优先级来自复核报告。</p></div>
      <button class="ligand-button" onClick={() => setState('expanded', !state.expanded)}>{state.expanded ? '退出大图' : '展开结果总览'}</button></header>
    <p>阿法替尼对照重对接 RMSD 1.29 Å。候选最佳评分姿势全部通过结构检查；70 个姿势中 69 个通过，007 的索引 9 被排除。</p>
    <Show when={sdk.projectID === 'prj_dbfd2a34da2e4c78977238fcf59d39ae'}><a class="ligand-button" href={`/${sdk.projectID}/discovery/de-novo`}>打开 De novo 工作台 · 最新进度与 MD 证据 →</a></Show>
    <div class="ligand-cards"><For each={props.rows}>{(candidate, i) => <article class="ligand-card" classList={{ 'is-selected': state.selected === i() }} data-denovo-card>
      <button class="ligand-card-select" aria-label={`查看候选 ${candidate.compound_id}`} onClick={() => setState('selected', i())}>
        <strong>{candidate.compound_id}</strong><span class="ligand-identity">{candidate.md_priority ? `MD 优先级 ${candidate.md_priority}${candidate.md_tier === 'exploratory' ? ' · 探索性' : ''}` : '本轮未入选 MD'}</span></button>
      <ErrorBoundary fallback={<p>二维图加载失败，原始数据仍可下载。</p>}><Suspense fallback={<p>加载分子…</p>}><Chem2D kind="chem-2d" data={{ smiles: candidate.standardized_smiles }} height={170} /></Suspense></ErrorBoundary>
      <dl class="ligand-metrics"><div><dt>CovDock 评分</dt><dd>{number(candidate.covdock_score)}</dd></div><div><dt>局部应变 kcal/mol</dt><dd>{number(candidate.local_strain_energy)}</dd></div></dl>
    </article>}</For></div>
    <div class="ligand-evidence"><h3 data-candidate-identity>{row().compound_id}</h3>
      <p><strong>MD 决定：</strong>{row().md_reason}</p><p>{row().md_limitation}</p>
      <p>生成／对接骨架 RMSD：{number(row().generated_scaffold_rmsd_A)} Å；溶剂化全局应变：{number(row().solvated_global_strain_kcal_mol)} kcal/mol；氢键残基：{row().hydrogen_bond_residues || '未检出'}。</p>
      <p>原始生成图为反应前分子；CovDock 为共价加合物。RMSD 已在蛋白对齐后计算，以下查看器保留各自原始坐标，未做视觉叠合。这里保留 MD 前入选记录；后续 MD 进展与研究建议见工作台。结构通过和 MD 入选均不代表活性。</p>
    </div>
    <div class="ligand-methods" role="group" aria-label="生成与对接结构">
      <button class="ligand-button" aria-pressed={!state.compare && state.method === 'generated'} onClick={() => setState({ method: 'generated', compare: false })}>PocketXMol 生成姿势（反应前）</button>
      <button class="ligand-button" aria-pressed={!state.compare && state.method === 'covdock'} onClick={() => setState({ method: 'covdock', compare: false })}>CovDock 姿势（共价加合物）</button>
      <button class="ligand-button" aria-pressed={state.compare} onClick={() => setState('compare', !state.compare)}>并排比较</button>
    </div>
    <div style={{ display: 'grid', 'grid-template-columns': state.compare ? 'repeat(auto-fit, minmax(340px, 1fr))' : '1fr', gap: '12px' }}>
      <For each={state.compare ? ['generated', 'covdock'] as const : [state.method]}>{method => <div data-denovo-method={method}>
        <h3>{method === 'generated' ? '反应前生成姿势 · 虚线为预期成键位置' : `共价加合物 · 原始姿势索引 ${row().covdock_structure_pose}`}</h3>
        <ErrorBoundary fallback={(e, reset) => <p role="alert">{String(e)} <button onClick={reset}>重试查看</button></p>}><Suspense fallback={<p>读取并校验结构…</p>}><Structure row={row()} method={method} compact /></Suspense></ErrorBoundary>
      </div>}</For>
    </div>
  </section>
}
