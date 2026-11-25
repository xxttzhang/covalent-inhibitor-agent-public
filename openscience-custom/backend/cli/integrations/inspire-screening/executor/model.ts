import z from "zod"
import { createHash } from "node:crypto"
import { origin, requestHash } from "./request"
import { minutes, plannerVersion, policy, screeningMode } from "./resource-planner"
import { targetParameters } from "@synsci/util/screening-target"
import { assertTargetBinding } from "./target"

export const phase = z.enum(["prepare", "carsidock", "rtmscore", "report", "uncertainty", "af3", "covdock"])
export type Phase = z.infer<typeof phase>
export function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => [key, canonical(item)]))
  return value
}
export const digest = (value: unknown): string => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex")
const hash = z.string().regex(/^[a-f0-9]{64}$/)
const inputPath = z.string().startsWith("/inspire/").refine(value => !/[\\\x00-\x1f\x7f]/.test(value)
  && value.slice(1).split("/").every(part => part !== "" && part !== "." && part !== ".."), "Research inputs require canonical absolute Inspire file paths")
const inputFile = z.object({ path: inputPath, sha256: hash }).strict()
/** Host-frozen roles, never model-authored command arguments. The actual prefix
 * and the original complete library have separate byte identities. No defaults
 * are applied to historical plans that lack this optional extension. */
export const researchInputs = z.object({
  version: z.literal("screening-research-inputs-v1"),
  library: inputFile.extend({ records: z.literal(20000), selection: z.literal("frozen-file-order-prefix"),
    source: inputFile, manifest: inputFile }).strict(),
  receptor: inputFile, science_config: inputFile, reference: inputFile,
}).strict().superRefine((value, ctx) => {
  const files = [value.library, value.receptor, value.science_config, value.reference, value.library.manifest]
  if (new Set(files.map(item => item.path)).size !== files.length)
    ctx.addIssue({ code: "custom", message: "Research input roles must have distinct frozen file paths" })
})
export type ResearchInputs = z.infer<typeof researchInputs>
export const deliveryPolicy = z.object({ mode: z.literal("all-core-artifacts-v1"),
  failure: z.literal("diagnostics-and-verified-prefix-v1").optional(),
  max_file_bytes: z.number().int().min(1).max(128 * 1024 * 1024),
  max_total_bytes: z.number().int().min(1).max(1024 * 1024 * 1024),
  max_files: z.number().int().min(1).max(100000),
}).strict()
export const defaultDelivery = deliveryPolicy.parse({ mode: "all-core-artifacts-v1",
  failure: "diagnostics-and-verified-prefix-v1",
  max_file_bytes: 128 * 1024 * 1024, max_total_bytes: 1024 * 1024 * 1024, max_files: 100000 })
export const connection = z.object({ cli: z.string().startsWith("/"), account: z.string().min(1),
  notebook: z.string().min(1), workspace: z.literal("CPU资源空间") }).strict()
export const resource = z.object({ kind: z.enum(["hpc", "job"]), workspace: z.string().min(1),
  project: z.string().min(1), group: z.string().min(1), quota: z.string().regex(/^[01],[1-9][0-9]*,[1-9][0-9]*$/),
  image: z.string().min(1), image_type: z.enum(["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]),
  priority: z.number().int().min(1).max(10), points_per_hour: z.number().nonnegative(),
}).strict().superRefine((value, ctx) => {
  if ((value.kind === "hpc" && (value.workspace !== "CPU资源空间" || !value.quota.startsWith("0,"))) ||
      (value.kind === "job" && (value.workspace !== "分布式训练空间" || !value.quota.startsWith("1,")))) {
    ctx.addIssue({ code: "custom", message: "Workload, workspace and single-node quota do not agree" })
  }
})
export const step = z.object({ phase, minutes: z.number().int().positive().max(10080), resource,
  max_candidates: z.number().int().positive().max(20000) }).strict()
export const automationPlan = z.object({
  version: z.literal(plannerVersion), requested_mode: screeningMode, selected_mode: screeningMode,
  adjusted: z.boolean(), intent_digest: hash, estimated_points: z.number().finite().nonnegative(),
  budget_headroom: z.number().finite().nonnegative().nullable(),
  basis: z.literal("versioned-profile-plus-live-quota-rates"),
}).strict()
export const planSchema = z.object({
  version: z.literal("screening-executor-v1"), id: z.string().regex(/^screen-[a-f0-9]{24}$/),
  request: z.string().min(1).max(10000), directory: z.string().startsWith("/"), session: z.string().min(1),
  origin: origin.optional(),
  delivery: deliveryPolicy.optional(), // Never add a default to old immutable approvals.
  connection,
  target: z.literal("EGFR"), site: z.literal("C797"), library: z.literal("CovInDB/Virtual_Screening_Library.csv"),
  target_parameters: targetParameters.optional(), // No defaults or mutation of historical approvals.
  research_inputs: researchInputs.optional(),
  scope: z.enum(["bounded", "full"]), max_records: z.number().int().positive().nullable(),
  shortlist: z.number().int().positive().max(20000), seed: z.number().int().nonnegative(),
  assets: z.record(z.string().startsWith("/inspire/"), hash).refine((value) => Object.keys(value).length >= 5, "Frozen source, target, engine and worker assets are mandatory"),
  runtime: z.object({ project: z.string().startsWith("/inspire/"), root: z.string().startsWith("/inspire/"),
    worker: z.string().startsWith("/inspire/"), activation: z.string().startsWith("/inspire/") }).strict(),
  automation: automationPlan.optional(), // Old immutable approvals remain valid without an automation record.
  steps: z.array(step).length(4), max_points: z.number().finite().nonnegative().nullable(),
  gates: z.object({ min_carsidock_success: z.number().min(0.75).max(1),
    min_rtmscore_coverage: z.number().min(0.95).max(1) }).strict(),
  created: z.number().int().nonnegative(), approve_before: z.number().int().positive(),
  finish_before: z.number().int().positive(),
}).strict().superRefine((value, ctx) => {
  if (value.research_inputs) {
    if (value.automation?.requested_mode !== "research_pilot_v1" || value.automation.selected_mode !== "research_pilot_v1")
      ctx.addIssue({ code: "custom", message: "Explicit research inputs are restricted to the research pilot mode" })
    const roles = value.research_inputs
    for (const item of [roles.library, roles.receptor, roles.science_config, roles.reference, roles.library.manifest])
      if (value.assets[item.path] !== item.sha256)
        ctx.addIssue({ code: "custom", message: "Research input role differs from its frozen asset hash: " + item.path })
  }
  if (value.target_parameters) {
    try { assertTargetBinding(value.target, value.site, value.target_parameters) }
    catch (error) { ctx.addIssue({ code: "custom", message: error instanceof Error ? error.message : String(error) }) }
  }
  if (value.origin && value.origin.request_sha256 !== requestHash(value.request)) ctx.addIssue({ code: "custom", message: "Original request text differs from bound platform message" })
  if ((value.scope === "full") !== (value.max_records === null)) ctx.addIssue({ code: "custom", message: "Full scope cannot be truncated; bounded scope requires a row cap" })
  if (value.max_records !== null && value.shortlist > value.max_records) ctx.addIssue({ code: "custom", message: "Shortlist exceeds source scope" })
  if (JSON.stringify(value.steps.slice(0, 4).map((step) => step.phase)) !== JSON.stringify(["prepare", "carsidock", "rtmscore", "report"])) ctx.addIssue({ code: "custom", message: "Core execution order is fixed" })
  if (new Set(value.steps.map((step) => step.phase)).size !== value.steps.length) ctx.addIssue({ code: "custom", message: "Duplicate phases forbidden" })
  if (value.steps.some((step) => step.max_candidates > value.shortlist)) ctx.addIssue({ code: "custom", message: "Stage cap exceeds task shortlist" })
  if (value.steps.some((step) => step.max_candidates !== value.shortlist || step.resource.kind !== (["carsidock", "rtmscore"].includes(step.phase) ? "job" : "hpc"))) ctx.addIssue({ code: "custom", message: "Core stages preserve the full selected denominator and use matching compute types" })
  const total = value.steps.reduce((sum, step) => sum + step.minutes / 60 * step.resource.points_per_hour, 0)
  if (value.max_points === null && (!value.origin || value.automation?.selected_mode !== "test100" ||
    value.automation.requested_mode !== "test100")) ctx.addIssue({ code: "custom", message: "Uncapped budget requires a bound test100 request" })
  if (value.max_points !== null && total > value.max_points + 1e-8) ctx.addIssue({ code: "custom", message: "Sum of stage resource reservations exceeds approved points cap" })
  if (value.automation) {
    if ((value.automation.requested_mode === "research_pilot_v1") !== (value.automation.selected_mode === "research_pilot_v1")) {
      ctx.addIssue({ code: "custom", message: "Research pilot scope cannot be substituted with another screening mode" })
    }
    const selected = policy(value.automation.selected_mode)
    const plannedMinutes = minutes(selected)
    if (value.scope !== selected.scope || value.max_records !== selected.max_records || value.shortlist !== selected.shortlist ||
      value.steps.some((item) => item.minutes !== plannedMinutes[item.phase as keyof typeof plannedMinutes])) {
      ctx.addIssue({ code: "custom", message: "Automated resource plan differs from its versioned screening profile" })
    }
    if (value.automation.adjusted !== (value.automation.requested_mode !== value.automation.selected_mode)) {
      ctx.addIssue({ code: "custom", message: "Automated plan adjustment marker is inconsistent" })
    }
    if (Math.abs(value.automation.estimated_points - total) > 1e-8 ||
      (value.max_points === null ? value.automation.budget_headroom !== null :
        value.automation.budget_headroom === null || Math.abs(value.automation.budget_headroom - (value.max_points - total)) > 1e-8)) {
      ctx.addIssue({ code: "custom", message: "Automated plan cost no longer matches frozen live quota rates" })
    }
  }
  if (value.approve_before <= value.created || value.finish_before <= value.approve_before) ctx.addIssue({ code: "custom", message: "Invalid task deadlines" })
})
export type Plan = z.infer<typeof planSchema>
export type Step = z.infer<typeof step>
export const states = ["DRAFT", "APPROVED", "ACTIVE", "NEEDS_REVIEW", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "SUCCEEDED"] as const
export type State = typeof states[number]
export type StageState = "WAITING" | "DISPATCHING" | "SUBMITTED" | "QUEUED" | "RUNNING" | "VERIFYING" | "SUCCEEDED" | "FAILED" | "CANCELLED"
export type Stage = { task_id: string; ordinal: number; phase: Phase; name: string; state: StageState;
  handle: string | null; manifest: string | null; error: string | null; submitted: number | null }
export type Task = { id: string; plan: Plan; digest: string; state: State; approval: string | null;
  created: number; updated: number; lease_owner: string | null; lease_until: number; error: string | null }
export const outcome = z.object({ task_id: z.string(), phase, plan_digest: hash, status: z.enum(["SUCCEEDED", "FAILED"]),
  parent_digest: hash.nullable(), input_count: z.number().int().nonnegative(), success_count: z.number().int().nonnegative(),
  failed_count: z.number().int().nonnegative(), artifacts: z.record(z.string().refine((name) =>
    !/[\\\x00-\x1f\x7f]/.test(name) && name.split("/").every((part) => part !== "" && part !== "." && part !== ".."),
    "Artifact names must be contained relative paths"), hash),
  identity_digest: hash, gates: z.record(z.string(), z.boolean()), runtime_seconds: z.number().nonnegative(),
}).passthrough()
export type Outcome = z.infer<typeof outcome>

export function verifyOutcome(value: unknown, task: Task, stage: Stage, parent: Outcome | null) {
  const result = outcome.parse(value)
  if (result.task_id !== task.id || result.phase !== stage.phase || result.plan_digest !== task.digest) throw new Error("Output belongs to a different task/plan/stage")
  if (result.parent_digest !== (parent ? digest(parent) : null)) throw new Error("Output parent evidence does not match")
  if (result.input_count !== result.success_count + result.failed_count) throw new Error("Output identity denominator does not balance")
  if (result.status !== "SUCCEEDED" || !Object.keys(result.gates).length || Object.values(result.gates).some((value) => !value)) throw new Error("Scientific quality gate failed")
  if (!Object.keys(result.artifacts).length || result.success_count === 0) throw new Error("No verified successful artifacts")
  const contract = task.plan.steps[stage.ordinal]
  if (stage.phase === "prepare" && task.plan.automation?.selected_mode === "test100" && result.success_count !== 100)
    throw new Error("test100 requires 100 quality-checked candidates; stop for review instead of silently shrinking or filling the cohort")
  if (result.input_count > contract.max_candidates) throw new Error("Output exceeds approved candidate scope")
  if (parent && (result.identity_digest !== parent.identity_digest || result.input_count !== parent.input_count)) throw new Error("Molecular identity or denominator changed across stages")
  return result
}
