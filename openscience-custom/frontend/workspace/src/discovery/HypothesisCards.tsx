import { For, Show } from "solid-js"
import { hypothesisDisplay } from "@synsci/util/discovery-hypothesis-display"

export function HypothesisCards(props: { original: string }) {
  const view = () => hypothesisDisplay(props.original)
  return <div>
    <p>{view().warning}</p>
    <div class="hypothesis-cards" style={{ display: "grid", "grid-template-columns": "repeat(auto-fit, minmax(min(100%, 360px), 1fr))", gap: "16px" }}>
      <section><h4>靶点依据</h4><p>{view().rationale}</p></section>
      <section><h4>文献证据</h4><p>展开每条判断，查看对应的原始文献。</p>
        <For each={view().claims}>{claim => <details><summary>{claim.statement}</summary>
          <For each={claim.evidence}>{item => <p>{item.id} · <Show when={item.reference?.url} fallback={item.reference?.title ?? "未提供参考文献记录"}>{url => <a href={url()} target="_blank" rel="noopener noreferrer">{item.reference?.title}</a>}</Show></p>}</For>
        </details>}</For>
        <details><summary>全部原始参考文献（{view().references.length}）</summary><For each={view().references}>{item => <p>{item.id} · <Show when={item.url} fallback={item.title}>{url => <a href={url()} target="_blank" rel="noopener noreferrer">{item.title}</a>}</Show></p>}</For></details>
      </section>
      <section><h4>位点选择</h4><For each={view().site}>{line => <p>{line}</p>}</For></section>
      <section><h4>路线建议</h4><p>{view().route}</p><p>路线理由见原始依据；未提供跨路线比较时不推断优劣。</p></section>
      <section><h4>当前限制</h4><For each={view().missing} fallback={<p>原始产物未列出限制，不代表不存在限制。</p>}>{line => <p>{line}</p>}</For></section>
    </div>
  </div>
}
