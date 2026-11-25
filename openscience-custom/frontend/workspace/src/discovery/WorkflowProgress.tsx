import { A } from "@solidjs/router"
import { createResource, For, onCleanup, onMount, Show } from "solid-js"
import { useSDK } from "@/context/sdk"
import { workflowObservations, type WorkflowObservation } from "@synsci/util/discovery-workflows"

export function useWorkflowObservations() {
  const sdk = useSDK()
  const [result, { refetch }] = createResource(
    () => sdk.projectID,
    async (projectID) => {
      try {
        const response = await sdk.request("/project/workflows", {
          cache: "no-store",
          signal: AbortSignal.timeout(20000),
        })
        if (!response.ok) throw Error("研究状态读取失败")
        const value = workflowObservations.parse(await response.json())
        if (value.projectID !== projectID) throw Error("研究状态与当前项目不匹配")
        return { workflows: value.workflows, error: "" }
      } catch {
        return { workflows: [] as WorkflowObservation[], error: "无法读取聊天研究状态，请重试。" }
      }
    },
  )
  onMount(() => {
    const timer = setInterval(() => {
      if (!document.hidden && !result.loading) void refetch()
    }, 15000)
    onCleanup(() => clearInterval(timer))
  })
  return { result, refetch }
}

const stages: Record<string, string> = {
  INTAKE: "研究输入",
  TARGET_REVIEW: "靶点审阅",
  PARENT_REVIEW: "母体比较",
  STRUCTURE_REVIEW: "结构审阅",
  STRUCTURE_SELECTION: "结构选择",
  RECEPTOR_INPUT_READY: "受体输入已准备",
  RECEPTOR_PLAN_READY: "受体准备计划待执行",
  RECEPTOR_PREPARATION: "受体准备阶段",
  SCREENING_PLAN: "筛选计划",
  SCREENING_PLAN_READY: "筛选计划待执行",
  SCREENING_EXECUTION: "筛选执行阶段",
  SCREENING_REVIEW: "筛选结果审阅",
  CANDIDATE_REVIEW: "候选审阅",
  PARENT_COMPARISON_REVIEW_READY: "母体比较待审阅",
  EVIDENCE_PENDING: "证据获取中",
  EVIDENCE_FAILED: "证据获取失败",
  DESIGN_INPUTS_READY: "设计输入已准备",
  POSE_PLAN_READY: "构象计划待执行",
  POSE_EXECUTION_RUNNING: "构象执行阶段",
  POSE_EXECUTION_REVIEW: "构象结果审阅",
  BRANCH_REPORTED: "分支报告已保存",
  MD_GRAPH_RUNNING: "共价图检查阶段",
  MD_PARAMETERIZATION_RUNNING: "参数化阶段",
  MD_SYSTEM_PREPARATION_RUNNING: "MD 系统准备阶段",
  MD_SHORT_QC_RUNNING: "MD 短程质控阶段",
  MD_EQUILIBRATION_RUNNING: "MD 平衡阶段",
  DESIGN_READY: "设计准备",
  RECEPTOR_REVIEW: "受体审阅",
  DIRECT_LO_READY: "小批量直接 LO 准备",
  RECEPTOR_PREPARATION_REVIEW: "受体准备结果审阅",
  COVDOCK_EXECUTION_RUNNING: "CovDock 计算中",
  COVDOCK_EXECUTION_REVIEW: "CovDock 结果审阅",
}

export default function WorkflowProgress(props: {
  progress: ReturnType<typeof useWorkflowObservations>
  route?: "ligand-first" | "library-first" | "de-novo"
}) {
  const sdk = useSDK()
  const items = () =>
    (props.progress.result()?.workflows ?? []).filter((item) => !props.route || item.route === props.route)
  return (
    <section class="discovery-workflow-progress" aria-label="聊天研究进度">
      <div class="discovery-section-title">
        <h2>聊天研究</h2>
        <button
          class="discovery-link"
          disabled={props.progress.result.loading}
          onClick={() => void props.progress.refetch()}
        >
          刷新记录 ↻
        </button>
      </div>
      <p>显示原生工具最后保存的回执，不代表实时计算状态或验收通过。继续研究和查看完整证据请打开对应会话。</p>
      <Show when={props.progress.result()?.error}>
        <p role="alert">{props.progress.result()?.error}</p>
      </Show>
      <Show when={props.progress.result.loading && !props.progress.result()}>
        <p>读取研究记录…</p>
      </Show>
      <For each={items()}>
        {(item) => (
          <article class="discovery-workflow-record">
            <h3>
              <A href={`/${sdk.projectID}/session/${item.sessionID}`}>{item.title}</A>
            </h3>
            <p>
              {item.route === "ligand-first" ? "Ligand-first" : "Library-first"} ·{" "}
              {item.active ? "会话绑定有效" : "已暂停绑定"}
            </p>
            <code>{item.workflowID}</code>
            <Show
              when={item.observed}
              fallback={<p>已找到研究绑定，但最近会话中没有可验证的阶段回执。请打开会话读取原生状态。</p>}
            >
              {(observed) => (
                <>
                  <p>
                    <strong>{stages[observed().stage] ?? observed().stage}</strong> · 修订 {observed().revision}
                    <Show when={observed().target}>
                      {" "}
                      · {observed().target} {observed().site}
                    </Show>
                  </p>
                  <p>
                    记录时间：{new Date(observed().time).toLocaleString()} · 本次回执工件引用 {observed().artifacts}
                  </p>
                  <Show when={observed().execution}>
                    <p>
                      回执执行状态：<code>{observed().execution}</code>
                    </p>
                  </Show>
                  <Show when={observed().candidates}>
                    {(batch) => <div>
                      <p>生成 {batch().generated} 个 · 化学准入 {batch().admitted} 个 · 暂缓 {batch().deferred.length} 个</p>
                      <p>化学准入仅表示可进入对接，不代表姿势质量或活性通过。</p>
                      <Show when={batch().deferred.length}>
                        <details><summary>查看暂缓原因</summary>
                          <ul><For each={batch().deferred}>{row => <li><code>{row.id}</code>：{row.reason === "UNRESOLVED_STEREOCHEMISTRY" ? "立体化学未明确" : row.reason}</li>}</For></ul>
                        </details>
                      </Show>
                    </div>}
                  </Show>
                </>
              )}
            </Show>
            <Show when={item.requestFailed}>
              <p role="status">最近一次模型请求未完成。已有研究记录仍保留，请进入会话查看错误并恢复。</p>
            </Show>
            <A class="discovery-link" href={`/${sdk.projectID}/session/${item.sessionID}`}>
              打开研究与结果 →
            </A>
          </article>
        )}
      </For>
      <Show when={!props.progress.result.loading && !props.progress.result()?.error && !items().length}>
        <p>{props.route === "de-novo" ? "De novo 的研究计划在统一研究中查看。" : "当前项目尚无这类聊天工作流记录。"}</p>
      </Show>
    </section>
  )
}
