// Creates a real frozen draft + four Inspire CLI dry-runs. Does not approve,
// start a daemon, call a model API or allocate GPU/CPU workloads.
import { execute } from "./tool"
import { root, open } from "./host"
import { summary } from "./tool"
import type { ToolContext } from "@synsci/plugin/tool"

const boundary = new Error("No compute approval in preflight acceptance")
const observed: { task?: string; asked: boolean } = { asked: false }
const context = { directory: root, sessionID: "executor-preflight-acceptance", abort: new AbortController().signal,
  metadata: (value: Parameters<ToolContext["metadata"]>[0]) => { observed.task = value.metadata?.task_id },
  ask: async () => { observed.asked = true; throw boundary } } as unknown as ToolContext
await execute({ action: "create", request: "验收持久化执行器：从 CovInDB 虚拟筛选库前 10000 条重新准备，最多 5 个候选，依次运行 CarsiDock、RTMScore 并交付验收报告；提交前审批。",
  target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv", scope: "bounded", max_records: 10000,
  shortlist: 5, prepare_minutes: 30, carsidock_minutes: 20, rtmscore_minutes: 10, report_minutes: 5, max_points: 0.5 }, context)
  .catch((error) => { if (error !== boundary) throw error })
if (!observed.asked || !observed.task) throw new Error("Create failed to request approval automatically")
const store = await open()
const result = summary(store.get(observed.task), store.stages(observed.task))
store.close()
if (result.state !== "DRAFT" || result.stages?.some((step) => step.state !== "WAITING")) throw new Error("Preflight unexpectedly dispatched")
await Bun.write(root + "/reports/inspire-executor-preflight.json", JSON.stringify({ tested_at: new Date().toISOString(),
  ...result, approval_requested: observed.asked, submitted: false, gpu_execution_verified: false, model_api_called: false }, null, 2))
console.log(JSON.stringify({ task_id: result.task_id, state: result.state, stages: result.stages?.map((step) => ({ phase: step.phase, state: step.state })),
  max_points: result.max_points, submitted: false }))
