import type { MessageV2 } from "./message-v2"
import { parseTargetParameters, targetCommand } from "@synsci/util/screening-target"

export const screeningTool = "inspire_screening_task"

// Only direct, unquoted user commands enter the deterministic executor. The
// executor independently binds the original message and confirms all conditions.
// This selects a tool; it neither grants resources nor changes scientific scope.
export function screeningEntry(parts: MessageV2.Part[], agent: string) {
  if (!["research", "biology", "build"].includes(agent)) return false
  if (parts.some((part) => part.type === "file" || part.type === "agent")) return false
  const text = parts.filter((part) => part.type === "text" && !part.synthetic && !part.ignored)
    .map((part) => part.type === "text" ? part.text : "").join("\n").trim()
  if (!text || text.length > 1000) return false
  // Only the exact form-generated command bypasses the prose classifier.
  // JSON quotes do not relax the refusal of negation, history or quoted examples.
  if (text.includes("target_parameters=")) {
    try {
      const parameters = parseTargetParameters(text)
      const mode = text.match(/screening_mode=(economy|standard|deep|test100|research_pilot_v1)；/)
      const points = text.match(/；预算(\d+(?:\.\d+)?)点券\n/)
      return !!parameters && !!mode && !!points && text === targetCommand(parameters, mode[1], Number(points[1]))
    } catch { return false }
  }
  if (/[?？`<>“”「」"']|(^|\n)\s*>|为什么|如何|怎么|请问|是否|可以吗|能否|解释|介绍|讨论|方案|设计|实现|开发|修改|例如|示例|之前|历史|取消|停止|不要|不用|无需|不能|不做|禁止|不运行|不执行|不启动|暂不|只查询|仅查询|先别|全库|天然产物|已知抑制剂|AF3|CovDock|\b(?:not|example|previous|instead|or)\b/i.test(text)) return false
  return /\bEGFR\b/i.test(text) && /\bC797\b/i.test(text) &&
    /(?:CovInDB|CovalentInDB).*?(?:虚拟筛选库|Virtual_Screening_Library\.csv)/is.test(text) &&
    /(?:筛选|执行|启动|运行)/.test(text)
}

export function screeningReply(output: unknown) {
  if (!output || typeof output !== "object" || !("output" in output) || typeof output.output !== "string")
    return "筛选工具已返回，请查看工具卡片中的实际状态。"
  try {
    const result = JSON.parse(output.output)
    if (result.state === "TARGET_CONFIGURATION_REQUIRED")
      return `任务参数已保存（${result.intake_id}）。${result.reason} 当前未启动计算。`
    if (typeof result.task_id !== "string" || !/^screen-[a-f0-9]{24}$/.test(result.task_id))
      return "筛选工具已返回，请查看工具卡片中的实际状态。"
    return `任务已登记：${result.task_id}，当前状态：${result.state}。\n\n后续准备/QC → CarsiDock → RTMScore → 报告由后台执行器推进，不需要重复发指令。请以任务卡实时状态为准；登记或批准不代表作业已启动。AF3/CovDock 精筛尚未接通，不会自动替代或声称完成。`
  } catch {
    return "筛选工具已返回，请查看工具卡片中的实际状态。"
  }
}
