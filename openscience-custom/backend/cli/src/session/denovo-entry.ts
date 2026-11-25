import type { MessageV2 } from "./message-v2"
import { matchRouteIntent } from "./route-workflow"

export const denovoTool = "denovo_generate"

export function denovoIntent(text: string) {
  return matchRouteIntent(text) === "de-novo"
}

// This deliberately narrow grammar is a registered command, not an intent guess.
// Questions, quoted examples, mutations, other targets and extra constraints do
// not grant computation. They continue through the normal conversation path.
export function denovoRequest(text: string) {
  const match = text.trim().match(/^(?:请|帮我|请帮我)?\s*(?:针对|面向|为)?\s*EGFR\s*(?:WT\s*)?C797\s*[，,：:]?\s*(?:固定|使用|采用)\s*(?:丙烯酰胺|acrylamide)\s*[，,；;]?\s*(?:(?:用|使用)\s*PocketXMol\s*)?(?:从头生成|de[_\s-]?novo\s*生成)\s*(?:(\d+)\s*个)?\s*(?:候选分子|候选小分子|小分子|候选)?\s*[。.!！]?$/i)
  if (!match) return
  const slots = match[1] ? Number(match[1]) : 32
  if (!Number.isSafeInteger(slots) || slots < 1 || slots > 96) return
  return { profile: "egfr-c797-acrylamide-v1", slots }
}

export function denovoStudyCommand(text: string) {
  const match = text.trim().match(/^执行 de-novo 研究 (study-[a-f0-9-]{36})$/)
  return match ? { action: "create_study" as const, study_id: match[1] } : undefined
}

export function denovoEntry(parts: MessageV2.Part[], agent: string) {
  if (!["research", "biology", "build"].includes(agent) || parts.some((p) => p.type === "file" || p.type === "agent")) return false
  const text = parts.filter((p) => p.type === "text" && !p.synthetic && !p.ignored).map((p) => p.type === "text" ? p.text : "").join("\n")
  // Natural generation requests need scientific planning through the agent.
  // Only an explicit frozen-study execution command may bypass that loop.
  return !!denovoStudyCommand(text)
}

export const denovoPlanningInstructions = `For De novo requests, use the bound study workflow (follow denovo_generate/workflow's \`next\` action) rather than legacy denovo_generate/create.
- New vs. resume: create a new study with user-provided target, site, receptor, pocket, warhead, and constraints (ask if missing; never substitute EGFR defaults or invent citations). Resume an existing study only when requested or bound to this session; never create a replacement study to bypass a failed or interrupted step.
- Multi-warhead campaigns: declare \`denovoBranches\` on the parent study, materialize with \`branch_study\`, and pass explicit child study IDs for reasoning/planning/execution. Keep each branch's evidence, atom maps, and thresholds isolated; never rank different warheads by raw uncalibrated docking scores.
- Reasoning & execution: run evidence collection through \`discovery_study/start-reasoning\` (or \`resume-reasoning\` if interrupted) in the original scientific session, then \`sync-hypotheses\`, \`plan_study\`, and \`create_study\` for native approval. Only use \`denovo_generate/retry_study\` when generation is \`NEEDS_REVIEW\` due to an Inspire project quota rejection and freshly confirmed \`JobNotFound\`.`

export function denovoReply(output: unknown) {
  if (output && typeof output === "object" && "output" in output && typeof output.output === "string") return output.output
  return "从头生成工具已返回，请查看工具卡片中的实际状态。"
}
