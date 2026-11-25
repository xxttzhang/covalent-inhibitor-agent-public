import { createMemo, For, Show } from "solid-js"
import { inspireSelection, selectionChannels } from "./inspire-selection"

export function InspireSelectionCard(props: { value: unknown; task: string; digest: string }) {
  const data = createMemo(() => inspireSelection(props.value, props.task, props.digest))
  const available = createMemo(() => { const value = data(); return value && value.state !== "UNAVAILABLE" ? value : undefined })
  return <Show when={data()}>{(value) => <section aria-label="已保存的精筛候选提案" style={{ display: "grid", gap: "8px", "border-top": "1px solid var(--border-weak-base)", "padding-top": "12px" }}>
    <strong>精筛候选提案 · {value().state === "UNAVAILABLE" ? "证据待检查" : value().state === "NO_CANDIDATES_MEET_POLICY" ? "无候选符合策略" : "待复核"}</strong>
    <p>本提案不授予精筛资源权限。新增预算：未在提案中确定（不是 0 点券）；AF3/CovDock 须另行审批。</p>
    <Show when={value().state === "UNAVAILABLE"}><p>{(() => { const item = value(); return item.state === "UNAVAILABLE" ? item.reason : "" })()}</p></Show>
    <Show when={available()}>{(item) => <>
      <p>核心输入 {item().input_count} · 可供选择 {item().eligible_count} · 排除 {item().excluded_count} · 提议 {item().selected_count} / 上限 {item().effective_cap}</p>
      <p>排名分歧阈值 ≥ {item().policy.disagreement_threshold}；领先范围 ≤ {item().policy.top_fraction}；比例上限 {item().policy.max_fraction * 100}%</p>
      <p>各类名额：分歧复核 {item().policy.uncertainty_count} · 一致性对照 {item().policy.consensus_count} · 多样性对照 {item().policy.diversity_count}</p>
      <Show when={item().selected_count === 0}><p>没有自动补满名额，也没有扩大候选池。调整策略需另行提出明确请求。</p></Show>
      <Show when={item().candidates.length}>
        <details><summary>查看全部 {item().selected_count} 个候选及理由</summary>
          <div style={{ overflow: "auto" }}><table style={{ width: "100%", "text-align": "left", "border-spacing": "0 8px" }}>
            <caption>排名百分位越小越靠前；排名分歧不是活性或不确定性概率。</caption>
            <thead><tr><th>候选 / 原始索引</th><th>选择理由</th><th>CarsiDock 排名</th><th>RTMScore 排名</th><th>排名分歧</th></tr></thead>
            <tbody><For each={item().candidates}>{(row) => <tr>
              <td style={{ "overflow-wrap": "anywhere" }}>{row.compound_id} [{row.input_sdf_index}]</td>
              <td>{selectionChannels[row.selection_channel]}</td><td>{row.carsidock_rank_fraction.toFixed(3)}</td>
              <td>{row.rtmscore_rank_fraction.toFixed(3)}</td><td>{row.rank_disagreement.toFixed(3)}</td>
            </tr>}</For></tbody>
          </table></div>
        </details>
      </Show>
      <small style={{ "overflow-wrap": "anywhere" }}>选择编号：{item().selection_id} · 清单校验：{item().selection_manifest_digest}</small>
      <small>{item().note}</small>
    </>}</Show>
    <small>提案记录于 {new Date(value().recorded_at).toLocaleString()}；本卡不报告精筛作业状态。</small>
  </section>}</Show>
}
