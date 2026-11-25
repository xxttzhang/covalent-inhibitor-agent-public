import { createMemo, For, Show } from "solid-js"
import { Card } from "./card"
import { deliveryLabel, inspireProgress, progressLabels, progressStageLabel } from "./inspire-progress"
import { useData } from "../context"
import { InspireSelectionCard } from "./inspire-selection-card"
import { InspirePreparationCard } from "./inspire-preparation-card"

export function InspireProgressCard(props: { value: unknown; results?: unknown; delivery?: unknown; total?: unknown; selection?: unknown; preparation?: unknown }) {
  const actions = useData()
  const data = createMemo(() => {
    const value = inspireProgress(props.value)
    return value && inspireProgress({ ...value, results: props.results ?? value.results,
      delivery: props.delivery ?? value.delivery, results_total: props.total ?? value.results_total })
  })
  return <Show when={data()}>{(value) =>
    <Card variant={value().state === "FAILED" || value().state === "NEEDS_REVIEW" ? "warning" : "info"}>
      <section data-component="inspire-progress" aria-label="Inspire 筛选任务进度" aria-live="polite" style={{ padding: "12px", display: "grid", gap: "8px" }}>
        <strong>Inspire 筛选进度 · {progressLabels[value().state]}</strong>
        <span>{value().target} · CovInDB 虚拟筛选库</span>
        <code style={{ "overflow-wrap": "anywhere" }}>{value().task_id}</code>
        <div style={{ overflow: "auto" }}>
          <table style={{ width: "100%", "text-align": "left", "border-spacing": "0 8px" }}>
            <thead><tr><th>阶段</th><th>状态</th><th>输入 / 成功 / 失败</th></tr></thead>
            <tbody><For each={value().stages}>{(stage) => <tr>
              <td>{progressLabels[stage.phase]}</td><td>{progressStageLabel(value().state, stage.state)}</td>
              <td>{stage.input_count ?? "—"} / {stage.success_count ?? "—"} / {stage.failed_count ?? "—"}</td>
            </tr>}</For></tbody>
          </table>
        </div>
        <Show when={value().error}><p>{value().error}</p></Show>
        <Show when={["FAILED", "NEEDS_REVIEW"].includes(value().state)}>
          <InspirePreparationCard value={props.preparation !== undefined ? props.preparation : value().preparation_diagnostic}
            task={value().task_id} digest={value().plan_digest} />
        </Show>
        <InspireSelectionCard value={props.selection !== undefined ? props.selection : value().selection}
          task={value().task_id} digest={value().plan_digest} />
        <Show when={value().delivery}>{(delivery) => <section aria-label="自动文件交付进度">
          <strong>文件交付 · {deliveryLabel(delivery().scope, delivery().state)}</strong>
          <p>{delivery().completed_files} / {delivery().total_files ?? "—"} 个文件 · {delivery().total_bytes} 字节</p>
          <Show when={delivery().error}><p>{delivery().error}</p></Show>
          <Show when={delivery().scope === "failure_evidence"}><p>只包含已验证的上游文件和失败诊断文件；不代表主筛完成，不包含未核验的候选结构。</p></Show>
          <small>交付状态独立于计算状态；暂停交付不会重跑计算。完成记录是当时的核验，不保证文件今后未被移除。</small>
        </section>}</Show>
        <Show when={value().results?.length}>
          <section aria-label="已保存的筛选文件" style={{ display: "grid", gap: "6px" }}>
            <strong>已保存到 Results 的文件</strong>
            <For each={value().results}>{(result) => <div>
              <button type="button" disabled={!actions.openArtifact} onClick={() => actions.openArtifact?.(result.artifact_id)}>
                查看文件及历史版本 · {result.phase}/{result.name}
              </button>
              <small> · {result.bytes} 字节 · 核验于 {new Date(result.published_at).toLocaleString()}</small>
              <Show when={result.evidence_kind === "failure_diagnostic"}><small> · 失败诊断，非科学结果</small></Show>
              <Show when={result.evidence_kind === "verified_prefix"}><small> · 已验证上游文件，整体任务仍失败</small></Show>
              <small style={{ display: "block", "overflow-wrap": "anywhere" }}>已核验版本：{result.version_id} · SHA256 {result.sha256}</small>
            </div>}</For>
            <small>显示 {value().results?.length} / {value().results_total ?? value().results?.length} 个已保存文件；完整列表与回收站请在 Results 查看。整套交付以文件交付状态为准。</small>
          </section>
        </Show>
        <small>状态同步于 {new Date(value().synced_at).toLocaleString()}；原始调用结果保留在上方。</small>
        <small>{value().note}</small>
      </section>
    </Card>
  }</Show>
}
