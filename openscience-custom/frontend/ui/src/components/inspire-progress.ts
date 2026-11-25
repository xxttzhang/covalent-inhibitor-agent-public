type Snapshot = { version: 1; task_id: string; plan_digest: string; state: string; target: string;
  library: string; error: string | null; synced_at: number; note: string;
  results_total?: number;
  selection?: unknown;
  preparation_diagnostic?: unknown;
  delivery?: { state: string; scope?: "core_results" | "failure_evidence"; total_files: number | null; completed_files: number; total_bytes: number;
    error: string | null; updated: number | null } | null;
  results?: { phase: string; name: string; artifact_id: string; version_id: string;
    evidence_kind?: "core_result" | "failure_diagnostic" | "verified_prefix";
    project_id: string; sha256: string; bytes: number; published_at: number }[];
  stages: { phase: string; state: string; job: string | null; input_count: number | null;
    success_count: number | null; failed_count: number | null; error: string | null }[] }
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value)
const count = (value: unknown) => value === null || (typeof value === "number" && Number.isSafeInteger(value) && value >= 0)
const text = (value: unknown) => value === null || typeof value === "string"
function valid(value: unknown): value is Snapshot {
  if (!record(value) || value.version !== 1 || typeof value.task_id !== "string" || !/^screen-[a-f0-9]{24}$/.test(value.task_id) ||
    typeof value.plan_digest !== "string" || !/^[a-f0-9]{64}$/.test(value.plan_digest) || typeof value.state !== "string" ||
    !["DRAFT", "APPROVED", "ACTIVE", "NEEDS_REVIEW", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "SUCCEEDED"].includes(value.state) ||
    typeof value.target !== "string" || typeof value.library !== "string" || typeof value.note !== "string" || !text(value.error) ||
    typeof value.synced_at !== "number" || !Number.isSafeInteger(value.synced_at) || value.synced_at <= 0 ||
    !Array.isArray(value.stages) || value.stages.length !== 4) return false
  if (value.results_total !== undefined && (typeof value.results_total !== "number" || !Number.isSafeInteger(value.results_total) || value.results_total < 0)) return false
  if (value.delivery !== undefined && value.delivery !== null) {
    const item = value.delivery
    if (!record(item) || !["WAITING", "RUNNING", "PAUSED", "COMPLETE"].includes(String(item.state)) ||
      (item.scope !== undefined && !["core_results", "failure_evidence"].includes(String(item.scope))) ||
      (item.scope === "failure_evidence" && value.state !== "FAILED") ||
      !count(item.total_files) || item.completed_files === null || !count(item.completed_files) ||
      item.total_bytes === null || !count(item.total_bytes) || !text(item.error) || !count(item.updated) ||
      (typeof item.total_files === "number" && Number(item.completed_files) > item.total_files) ||
      (item.state === "COMPLETE" && ((value.state !== "SUCCEEDED" && !(value.state === "FAILED" && item.scope === "failure_evidence")) ||
        item.total_files === null || item.completed_files !== item.total_files))) return false
  }
  if (value.results !== undefined && (!Array.isArray(value.results) || value.results.length > 10000 || !value.results.every((item) =>
    record(item) && ["prepare", "carsidock", "rtmscore", "report"].includes(String(item.phase)) && typeof item.name === "string" &&
    (item.evidence_kind === undefined || ["core_result", "failure_diagnostic", "verified_prefix"].includes(String(item.evidence_kind))) &&
    typeof item.artifact_id === "string" && /^art_[a-f0-9-]+$/.test(item.artifact_id) &&
    typeof item.version_id === "string" && /^ver_[a-f0-9-]+$/.test(item.version_id) &&
    typeof item.project_id === "string" && typeof item.sha256 === "string" && /^[a-f0-9]{64}$/.test(item.sha256) &&
    typeof item.bytes === "number" && Number.isSafeInteger(item.bytes) && item.bytes >= 0 &&
    typeof item.published_at === "number" && Number.isSafeInteger(item.published_at) && item.published_at > 0))) return false
  return value.stages.every((stage, index) => record(stage) && stage.phase === ["prepare", "carsidock", "rtmscore", "report"][index] &&
    typeof stage.state === "string" && ["WAITING", "DISPATCHING", "SUBMITTED", "QUEUED", "RUNNING", "VERIFYING", "SUCCEEDED", "FAILED", "CANCELLED"].includes(stage.state) &&
    text(stage.job) && text(stage.error) && count(stage.input_count) && count(stage.success_count) && count(stage.failed_count))
}

export const progressLabels: Record<string, string> = {
  DRAFT: "待审批", APPROVED: "已批准，待调度", ACTIVE: "执行中", NEEDS_REVIEW: "已暂停，需检查",
  CANCEL_REQUESTED: "等待取消确认", CANCELLED: "已取消", FAILED: "失败，后续未继续", SUCCEEDED: "已完成",
  WAITING: "待执行", DISPATCHING: "提交中", SUBMITTED: "已提交，等待状态确认", QUEUED: "排队或创建环境中", RUNNING: "实际运行中", VERIFYING: "核验输出中",
  prepare: "准备与质检", carsidock: "CarsiDock 主筛", rtmscore: "RTMScore 重评分", report: "核心验收报告",
}

export function inspireProgress(value: unknown) {
  return valid(value) ? value : undefined
}

export function progressStageLabel(task: string, stage: string) {
  if (stage !== "WAITING") return progressLabels[stage]
  if (task === "FAILED") return "未执行（任务已失败）"
  if (task === "CANCELLED") return "未执行（任务已取消）"
  if (task === "NEEDS_REVIEW") return "未执行（等待检查）"
  if (task === "CANCEL_REQUESTED") return "未执行（等待取消确认）"
  return progressLabels[stage]
}

export function deliveryLabel(scope: string | undefined, state: string) {
  if (scope === "failure_evidence") return ({ WAITING: "等待失败证据核验", RUNNING: "失败证据交付中",
    PAUSED: "失败证据交付暂停", COMPLETE: "失败证据清单已交付（筛选仍失败）" } as Record<string, string>)[state]
  return ({ WAITING: "等待核心流程完成", RUNNING: "交付中", PAUSED: "已暂停，尚未交付完整",
    COMPLETE: "完整清单已核验交付" } as Record<string, string>)[state]
}
