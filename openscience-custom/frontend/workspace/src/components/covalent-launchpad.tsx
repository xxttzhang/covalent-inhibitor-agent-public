import { For, Show, type JSX } from "solid-js"
import { IconActivity, IconArrowRight, IconAtom, IconDatabase, IconShield, IconSparkles } from "@/atlas/shared/Icon"
import type { CovalentDashboard, CovalentStageState } from "./covalent-dashboard"
import "./covalent-launchpad.css"
import { ScreeningTargetForm } from "./screening-target-form"

const routes = [
  {
    id: "library",
    index: "01",
    title: "Library-first",
    subtitle: "共价化合物库筛选",
    detail: "输入靶点与共价残基，智能体自动解析 Campaign，并从 CovalentInDB 分层筛选。",
    state: "从化合物库开始",
    input: "靶点 + 共价残基",
    output: "共识候选集",
    stages: ["Library", "Dock", "Rank"],
    icon: IconDatabase,
    prompt: "请先确认本次靶点、受体、链、残基编号与编号体系、反应类型，再从 CovInDB 虚拟筛选库创建筛选任务；不要套用其他任务的靶点参数。",
  },
  {
    id: "ligand",
    index: "02",
    title: "Ligand-first",
    subtitle: "先导骨架共价化",
    detail: "保留成熟非共价骨架，规划 attachment、linker 与 warhead，并进行受控生成。",
    state: "从非共价母体开始",
    input: "成熟骨架 + 构象",
    output: "共价类似物",
    stages: ["Anchor", "Linker", "Warhead"],
    icon: IconAtom,
    prompt: "启动 Ligand-first 路线。请读取现有骨架、结合构象和反应残基，生成可审计的共价化计划。",
  },
  {
    id: "denovo",
    index: "03",
    title: "De novo",
    subtitle: "口袋条件从头生成",
    detail: "依据口袋和反应几何进行结构生成、化学过滤、共价对接与独立评价。",
    state: "从结合口袋开始",
    input: "口袋 + 反应几何",
    output: "新颖化学空间",
    stages: ["Pocket", "Generate", "Audit"],
    icon: IconSparkles,
    prompt: "启动 De novo 路线。请检查口袋、共价残基与生成器资产，并规划第一次真实端到端生成。",
  },
] as const

const stateLabels: Record<CovalentStageState, string> = {
  completed: "完成",
  running: "运行中",
  queued: "排队",
  pending: "待运行",
  failed: "需处理",
}

const gateLabels: Record<string, string> = {
  rdkit_parse_rate_ge_0_99: "RDKit 解析率 ≥99%",
  selected_warhead_mapping_rate_ge_0_98: "候选 warhead 映射率 ≥98%",
  identity_loss_zero: "身份映射零丢失",
  failure_table_present: "失败记录完整",
  carsidock_success_rate_ge_minimum: "CarsiDock 成功率达标",
  selected_identity_unique: "候选身份唯一",
  selection_budget_satisfied_or_input_exhausted: "高召回池预算完整",
  coverage_ge_minimum: "AF3 有效覆盖率达标",
  selection_budget_satisfied_or_success_exhausted: "AF3 Top-N 输出完整",
  covdock_success_rate_ge_minimum: "CovDock-LO 成功率达标",
  final_candidates_available: "构象门控候选可用",
  baseline_objectives_available: "Pareto 基础证据完整",
  eligible_candidates_available: "实验候选可用",
  selection_budget_satisfied_or_eligible_exhausted: "实验候选预算完整",
}

function gateLabel(key: string) {
  const name = key.includes(":") ? key.slice(key.lastIndexOf(":") + 1) : key
  return gateLabels[name] ?? name
}

function formatCount(value: number | undefined) {
  if (value === undefined) return "—"
  return new Intl.NumberFormat("zh-CN").format(value)
}

function routeStatus(
  dashboard: CovalentDashboard | undefined,
  id: (typeof routes)[number]["id"],
  fallback: string,
) {
  if (id === "library" && dashboard?.library_first)
    return stateLabels[dashboard.library_first.state]
  const route = id === "ligand" ? "ligand-first" : id === "denovo" ? "de-novo" : "library-first"
  const tasks = dashboard?.tasks?.filter((task) => task.route === route) ?? []
  if (!tasks.length) return fallback
  if (tasks.some((task) => task.status === "running")) return "运行中"
  if (tasks.some((task) => task.status === "failed")) return "需处理"
  if (tasks.every((task) => task.status === "completed")) return "已完成"
  if (tasks.some((task) => task.status === "ready")) return "可启动"
  return "待运行"
}

export function CovalentLaunchpad(props: {
  onPrompt: (value: string) => void
  dashboard?: CovalentDashboard
  discoveryHref?: string
}): JSX.Element {
  const library = () => props.dashboard?.library_first
  return (
    <div class="covalent-launchpad">
      <div class="covalent-launchpad__glow" aria-hidden="true" />
      <div class="covalent-launchpad__constellation" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>
      <div class="covalent-launchpad__content">
        <header class="covalent-launchpad__hero">
          <div class="covalent-launchpad__hero-copy">
            <div class="covalent-launchpad__eyebrow">
              <span aria-hidden="true" />
              COVALENT DISCOVERY · AGENT WORKBENCH
            </div>
            <h2>
              从靶点出发，构建
              <span>可验证的共价候选</span>
            </h2>
            <p>选择一条研发路线开始。智能体会串联结构准备、分子生成、共价对接、证据审计与候选交接。</p>
            <div class="covalent-launchpad__metrics" aria-label="Platform capabilities">
              <span>
                <strong>03</strong>
                <small>研发路线</small>
              </span>
              <span>
                <strong>05</strong>
                <small>筛选引擎</small>
              </span>
              <span>
                <strong>01</strong>
                <small>候选档案</small>
              </span>
            </div>
          </div>

          <aside class="covalent-launchpad__system" aria-label="Discovery system status">
            <div class="covalent-orbit" aria-hidden="true">
              <span class="covalent-orbit__ring covalent-orbit__ring--outer" />
              <span class="covalent-orbit__ring covalent-orbit__ring--inner" />
              <span class="covalent-orbit__node covalent-orbit__node--one" />
              <span class="covalent-orbit__node covalent-orbit__node--two" />
              <span class="covalent-orbit__node covalent-orbit__node--three" />
              <span class="covalent-orbit__core">
                <IconAtom size={20} strokeWidth={1.4} />
              </span>
            </div>
            <div class="covalent-launchpad__system-panel">
              <div class="covalent-launchpad__system-title">
                <span aria-hidden="true" />
                DISCOVERY WORKBENCH
              </div>
              <div class="covalent-launchpad__system-track" aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
              <div class="covalent-launchpad__system-labels">
                <span>Structure</span>
                <span>Chemistry</span>
                <span>Evidence</span>
              </div>
            </div>
          </aside>
        </header>

        <section class="covalent-launchpad__route-section" aria-labelledby="covalent-routes-title">
          <div class="covalent-launchpad__section-heading">
            <div>
              <span>CHOOSE A STARTING POINT</span>
              <h3 id="covalent-routes-title">选择研发起点</h3>
            </div>
            <p>路线配置会自动写入对话框，你可以在发送前继续补充靶点、残基或项目约束。</p>
          </div>

          <div class="covalent-launchpad__routes">
            <For each={routes}>
              {(route) => {
                const RouteIcon = route.icon
                return (
                  <button
                    type="button"
                    class="covalent-route-card"
                    data-route={route.id}
                    onClick={() => props.onPrompt(route.prompt)}
                    aria-label={`Start ${route.title}: ${route.subtitle}`}
                  >
                    <span class="covalent-route-card__topline">
                      <span class="covalent-route-card__index">ROUTE / {route.index}</span>
                      <span class="covalent-route-card__state">
                        <span aria-hidden="true" />
                        {routeStatus(props.dashboard, route.id, route.state)}
                      </span>
                    </span>
                    <span class="covalent-route-card__icon" aria-hidden="true">
                      <RouteIcon size={21} strokeWidth={1.4} />
                    </span>
                    <span class="covalent-route-card__copy">
                      <strong>{route.title}</strong>
                      <span>{route.subtitle}</span>
                    </span>
                    <span class="covalent-route-card__detail">{route.detail}</span>
                    <span class="covalent-route-card__stages" aria-hidden="true">
                      <For each={route.stages}>
                        {(stage, index) => (
                          <>
                            <span>{stage}</span>
                            {index() < route.stages.length - 1 && <i />}
                          </>
                        )}
                      </For>
                    </span>
                    <span class="covalent-route-card__spec">
                      <span>
                        <small>INPUT</small>
                        <strong>{route.input}</strong>
                      </span>
                      <span>
                        <small>OUTPUT</small>
                        <strong>{route.output}</strong>
                      </span>
                    </span>
                    <span class="covalent-route-card__action">
                      启动路线
                      <IconArrowRight size={14} strokeWidth={1.6} />
                    </span>
                  </button>
                )
              }}
            </For>
          </div>
        </section>

        <Show when={props.discoveryHref}><a href={props.discoveryHref} style={{ display: "flex", "align-items": "center", "justify-content": "space-between", padding: "18px 22px", background: "#edf5f2", border: "1px solid #cfe1dc", "border-radius": "10px", color: "#246c61", "margin-bottom": "20px" }}><span><strong>三路线 · 研究进程与候选分子</strong><br /><small>切换 Library-first、Ligand-first 与 De novo，查看进度和证据</small></span><span>打开工作台 →</span></a></Show>
        <ScreeningTargetForm onPrompt={props.onPrompt} />

        <Show when={library()}>
          {(status) => (
            <section class="covalent-pipeline" aria-labelledby="library-pipeline-title">
              <header class="covalent-pipeline__header">
                <div>
                  <span>
                    LIVE WORKFLOW · {status().target}
                    <Show when={status().campaign_id}>{` · ${status().campaign_id}`}</Show>
                  </span>
                  <h3 id="library-pipeline-title">Library-first 实时工作流</h3>
                </div>
                <span class={`covalent-pipeline__state covalent-pipeline__state--${status().state}`}>
                  <i aria-hidden="true" />
                  {stateLabels[status().state]}
                  <Show when={status().next_stage}>{` · NEXT ${status().next_stage}`}</Show>
                </span>
              </header>

              <div class="covalent-pipeline__summary">
                <span>
                  <small>DATA SOURCE</small>
                  <strong>{status().source}</strong>
                </span>
                <span>
                  <small>PROCESSED</small>
                  <strong>{formatCount(status().processed)}</strong>
                </span>
                <span>
                  <small>SELECTED</small>
                  <strong>{formatCount(status().selected)}</strong>
                </span>
                <span>
                  <small>RUN ID</small>
                  <strong title={status().run_id}>{status().run_id}</strong>
                </span>
              </div>

              <div class="covalent-pipeline__progress" aria-label={`Pipeline progress ${status().progress}%`}>
                <span style={{ width: `${status().progress}%` }} />
              </div>

              <div class="covalent-pipeline__stages">
                <For each={status().stages}>
                  {(stage, index) => (
                    <div
                      class={`covalent-pipeline-stage covalent-pipeline-stage--${stage.status}`}
                      title={stage.run_id}
                    >
                      <span class="covalent-pipeline-stage__index">{String(index() + 1).padStart(2, "0")}</span>
                      <span class="covalent-pipeline-stage__copy">
                        <strong>{stage.label}</strong>
                        <small>
                          {stateLabels[stage.status]}
                          <Show when={stage.success_rate !== undefined}>
                            {` · ${(stage.success_rate! * 100).toFixed(1)}%`}
                          </Show>
                        </small>
                      </span>
                      <span class="covalent-pipeline-stage__count">{formatCount(stage.count)}</span>
                    </div>
                  )}
                </For>
              </div>

              <div class="covalent-pipeline__gates" aria-label="Quality gates">
                <For each={Object.entries(status().quality_gates)}>
                  {([key, passed]) => (
                    <span classList={{ "is-passed": passed, "is-failed": !passed }}>
                      <i aria-hidden="true" />
                      {gateLabel(key)}
                    </span>
                  )}
                </For>
              </div>
            </section>
          )}
        </Show>

        <footer class="covalent-launchpad__footer">
          <span class="covalent-launchpad__footer-icon" aria-hidden="true">
            <IconShield size={16} strokeWidth={1.5} />
          </span>
          <span class="covalent-launchpad__footer-copy">
            <small>UNIFIED EVIDENCE HANDOFF</small>
            <strong>所有路线统一汇入 Candidate Passport</strong>
          </span>
          <span class="covalent-launchpad__footer-flow" aria-hidden="true">
            <span>
              <IconActivity size={14} strokeWidth={1.5} />
              证据审计
            </span>
            <i />
            <span>独立评价智能体</span>
          </span>
        </footer>
      </div>
    </div>
  )
}
