import { For, Show, createMemo } from "solid-js"
import { preparationDiagnostic, propertyReasons } from "./inspire-preparation"

export function InspirePreparationCard(props: { value: unknown; task: string; digest: string }) {
  const data = createMemo(() => preparationDiagnostic(props.value, props.task, props.digest))
  return <Show when={data()}>{(value) => <section aria-label="准备阶段诊断" style={{ display: "grid", gap: "8px" }}>
    <strong>准备阶段诊断 · {value().state === "AVAILABLE" ? "已有记录" : "不可用"}</strong>
    <Show when={value().state === "AVAILABLE" && value().result}>{(result) => <>
      <p>源输入 {result().input_rows} 条，标准化独特记录 {result().rows} 条；通过全部硬门槛 {result().eligible} 个。</p>
      <small>本诊断仅覆盖获批的文件前缀，不是全库的随机或代表性抽样；不会自动扩大读取范围。</small>
      <p>无兼容反应基团 {result().incompatible} 个；电亲位点有歧义 {result().ambiguous} 个；兼容且位点无歧义 {result().valid_warhead} 个。</p>
      <Show when={result().eligible === 0}><strong>当前范围没有合格候选，后续 GPU 阶段未执行。不能据此推断全库无候选或分子无活性。</strong></Show>
      <p>冻结性质窗口：MW {result().limits.min_mw}–{result().limits.max_mw}；cLogP {result().limits.min_logp}–{result().limits.max_logp}；TPSA ≤ {result().limits.max_tpsa}；可旋转键 ≤ {result().limits.max_rotors}；|电荷| ≤ {result().limits.max_abs_formal_charge}。</p>
      <table><thead><tr><th>性质排除原因</th><th>全部独特记录</th><th>兼容且位点无歧义</th></tr></thead>
        <tbody><For each={Object.keys(propertyReasons)}>{(key) => <tr><td>{propertyReasons[key]}</td>
          <td>{result().property_failures[key]}</td><td>{result().valid_warhead_property_failures[key]}</td></tr>}</For></tbody></table>
      <small>同一分子可能违反多个性质条件，各行不可相加作为分子总数。</small>
      <code style={{ "overflow-wrap": "anywhere" }}>诊断表 SHA256：{result().files["master.parquet"].sha256}</code>
    </>}</Show>
    <small>{value().note}</small>
    <small>诊断记录时间：{new Date(value().recorded_at).toLocaleString()}</small>
  </section>}</Show>
}
