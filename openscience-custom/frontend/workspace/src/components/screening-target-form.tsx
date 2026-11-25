import { For, Show } from "solid-js"
import { createStore } from "solid-js/store"
import { targetCommand, targetParameters } from "@synsci/util/screening-target"

export function ScreeningTargetForm(props: { onPrompt: (value: string) => void }) {
  const [state, setState] = createStore({ target: "", receptor: "", chain: "", residue: "", residue_number: "",
    insertion_code: "", numbering: "canonical", reaction_type: "", mode: "standard", points: "", error: "" })
  function submit(event: SubmitEvent) {
    event.preventDefault()
    try {
      if (!state.residue_number.trim() || !state.points.trim()) throw new Error("请填写残基编号与点券上限。")
      const parameters = targetParameters.parse({ target: state.target, receptor: state.receptor, chain: state.chain,
        residue: state.residue, residue_number: Number(state.residue_number), insertion_code: state.insertion_code,
        numbering: state.numbering, reaction_type: state.reaction_type })
      props.onPrompt(targetCommand(parameters, state.mode, Number(state.points)))
      setState("error", "")
    } catch (error) { setState("error", error instanceof Error ? error.message : String(error)) }
  }
  return <section id="screening-target-parameters" class="covalent-launchpad__route-section screening-target">
    <h3>Library-first 任务参数</h3>
    <p>填写后生成待发送指令，不会立即启动算力。新靶点须先核验受体、编号映射和引擎；没有 EGFR/C797 默认值。</p>
    <form onSubmit={submit}>
      <For each={[
        ["target", "靶点名称", "例如 BTK"], ["receptor", "受体标识或路径", "PDB ID 或结构文件路径"],
        ["chain", "链（auth chain）", "例如 A；空白表示空链"], ["residue_number", "目标残基编号", "例如 481"],
        ["insertion_code", "插入码", "无则留空"], ["reaction_type", "反应类型", "例如 Michael Addition"],
      ] as const}>{([key, label, placeholder]) => <label>{label}
        <input aria-label={label} value={state[key]} placeholder={placeholder} required={!["chain", "insertion_code"].includes(key)}
          onInput={(e) => setState(key, e.currentTarget.value)} />
      </label>}</For>
      <label>残基类型<select aria-label="残基类型" required value={state.residue} onChange={(e) => setState("residue", e.currentTarget.value)}>
        <option value="">请选择</option><For each={["CYS", "SER", "LYS", "TYR", "THR", "HIS", "ASP", "GLU", "ALA", "ARG", "ASN", "GLN", "GLY", "ILE", "LEU", "MET", "PHE", "PRO", "TRP", "VAL"]}>{(r) => <option>{r}</option>}</For>
      </select></label>
      <label>编号体系<select aria-label="编号体系" value={state.numbering} onChange={(e) => setState("numbering", e.currentTarget.value)}>
        <option value="canonical">标准序列编号（需映射）</option><option value="structure">结构原始编号</option>
      </select></label>
      <label>筛选模式<select aria-label="筛选模式" value={state.mode} onChange={(e) => setState("mode", e.currentTarget.value)}>
        <option value="economy">经济筛选</option><option value="standard">标准筛选</option><option value="deep">深度筛选</option><option value="test100">100 候选测试</option>
      </select></label>
      <label>预算上限（点券）<input aria-label="预算上限（点券）" type="number" min="0" step="any" required value={state.points} onInput={(e) => setState("points", e.currentTarget.value)} /></label>
      <button type="submit">将任务参数写入对话框</button>
    </form>
    <Show when={state.error}><p role="alert">{state.error}</p></Show>
  </section>
}
