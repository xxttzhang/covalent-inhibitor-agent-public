import z from "zod"

export const screeningMode = z.enum(["economy", "standard", "deep", "test100", "research_pilot_v1"])
export type ScreeningMode = z.infer<typeof screeningMode>

export const plannerVersion = "screening-resource-planner-v1" as const

export const profile = z.object({
  scope: z.literal("bounded"),
  max_records: z.number().int().positive(),
  shortlist: z.number().int().positive().max(20000),
  prepare_minutes: z.number().int().positive().max(10080),
  carsidock_minutes: z.number().int().positive().max(10080),
  rtmscore_minutes: z.number().int().positive().max(10080),
  report_minutes: z.number().int().positive().max(60),
}).strict()

export type Profile = z.infer<typeof profile>

const profiles: Record<ScreeningMode, Profile> = {
  // The pilot has its own immutable scale. Its two sequential GPU reservations
  // total 15 minutes; CPU core-hours still depend on the host's concrete quota.
  // This is a per-task ceiling, not a substitute for the research ledger's
  // cumulative round, retry, point, model-token and elapsed-time limits.
  research_pilot_v1: { scope: "bounded", max_records: 20000, shortlist: 5,
    prepare_minutes: 15, carsidock_minutes: 10, rtmscore_minutes: 5, report_minutes: 5 },
  test100: { scope: "bounded", max_records: 100000, shortlist: 100,
    prepare_minutes: 60, carsidock_minutes: 120, rtmscore_minutes: 60, report_minutes: 10 },
  economy: { scope: "bounded", max_records: 2000, shortlist: 3,
    prepare_minutes: 15, carsidock_minutes: 10, rtmscore_minutes: 5, report_minutes: 5 },
  standard: { scope: "bounded", max_records: 10000, shortlist: 5,
    prepare_minutes: 30, carsidock_minutes: 20, rtmscore_minutes: 10, report_minutes: 5 },
  deep: { scope: "bounded", max_records: 50000, shortlist: 20,
    prepare_minutes: 60, carsidock_minutes: 60, rtmscore_minutes: 30, report_minutes: 10 },
}

export const label = (mode: ScreeningMode) => ({ economy: "经济", standard: "标准", deep: "深度", test100: "100候选测试", research_pilot_v1: "研究闭环试点 v1（2万条/最多5候选）" })[mode]

export function policy(mode: ScreeningMode) {
  return profile.parse(profiles[screeningMode.parse(mode)])
}

export type Rates = { cpu: number; gpu: number }

export function estimate(value: Profile, rates: Rates) {
  const checked = profile.parse(value)
  const parsed = z.object({ cpu: z.number().finite().nonnegative(), gpu: z.number().finite().nonnegative() }).strict().parse(rates)
  return ((checked.prepare_minutes + checked.report_minutes) * parsed.cpu +
    (checked.carsidock_minutes + checked.rtmscore_minutes) * parsed.gpu) / 60
}

export type Intent = {
  screening_mode: ScreeningMode
  requested_mode?: ScreeningMode
  max_points: number | null
}

export type Choice = {
  requested: ScreeningMode
  selected: ScreeningMode
  profile: Profile
  estimated_points: number
  max_points: number | null
  adjusted: boolean
  reason: "requested_mode_fits" | "lower_mode_fits" | "minimum_budget_required"
  requires_confirmation: boolean
}

// A transparent, versioned policy chooses scientific scale and stage time.
// Live quota rows still choose the actual resource identity and price. This is
// deliberately not called an empirical throughput model until successful runs
// provide measured calibration evidence.
export function choose(input: Intent, rates: Rates): Choice {
  const intent = z.object({ screening_mode: screeningMode, requested_mode: screeningMode.optional(),
    max_points: z.number().finite().nonnegative().nullable() }).strict().parse(input)
  if (intent.max_points === null && (intent.screening_mode !== "test100" ||
    (intent.requested_mode !== undefined && intent.requested_mode !== "test100")))
    throw new Error("No points cap is supported only for the bounded test100 profile")
  const requested = intent.requested_mode ?? intent.screening_mode
  const selected = intent.screening_mode
  if ((requested === "research_pilot_v1") !== (selected === "research_pilot_v1"))
    throw new Error("Research pilot scope cannot be substituted with another screening mode")
  const wanted = policy(selected)
  const cost = estimate(wanted, rates)
  if (intent.max_points === null || cost <= intent.max_points + 1e-8) return { requested, selected, profile: wanted,
    estimated_points: cost, max_points: intent.max_points, adjusted: requested !== selected,
    reason: requested === selected ? "requested_mode_fits" : "lower_mode_fits", requires_confirmation: false }

  if (selected === "test100" || selected === "research_pilot_v1") return { requested, selected, profile: wanted,
    estimated_points: cost, max_points: intent.max_points, adjusted: false,
    reason: "minimum_budget_required", requires_confirmation: true }
  const cap = intent.max_points
  const order: ScreeningMode[] = ["economy", "standard", "deep"]
  const candidates = order.slice(0, order.indexOf(selected)).reverse()
    .map((mode) => ({ mode, profile: policy(mode), cost: estimate(policy(mode), rates) }))
  const fallback = candidates.find((item) => item.cost <= cap + 1e-8)
  if (fallback) return { requested, selected: fallback.mode, profile: fallback.profile,
    estimated_points: fallback.cost, max_points: intent.max_points, adjusted: true,
    reason: "lower_mode_fits", requires_confirmation: true }

  const minimum = policy("economy")
  return { requested, selected: "economy", profile: minimum,
    estimated_points: estimate(minimum, rates), max_points: intent.max_points, adjusted: true,
    reason: "minimum_budget_required", requires_confirmation: true }
}

export function minutes(value: Profile) {
  return {
    prepare: value.prepare_minutes,
    carsidock: value.carsidock_minutes,
    rtmscore: value.rtmscore_minutes,
    report: value.report_minutes,
  }
}
