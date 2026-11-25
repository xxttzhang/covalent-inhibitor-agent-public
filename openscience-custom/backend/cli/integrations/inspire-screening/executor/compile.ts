import { randomBytes } from "node:crypto"
import type { z } from "zod"
import { conditions } from "./intake"
import { defaultDelivery, digest, planSchema, type Plan, resource } from "./model"
import { choose, minutes, plannerVersion } from "./resource-planner"
import type { BoundRequest } from "./request"
import { assertTargetBinding } from "./target"

/** Shared real plan compiler. Pure construction: no approvals, task storage,
 * resource allocation, SSH, or observer. Caller supplies verified live assets
 * and rates; compilation alone does not certify their freshness. */
export function compileScreening(input: {
  intent: z.infer<typeof conditions>; source: Pick<BoundRequest, "text"> & Partial<Pick<BoundRequest, "origin">>;
  directory: string; session: string;
  runtime: Plan["runtime"]; connection: Plan["connection"]; assets: Plan["assets"];
  research_inputs?: Plan["research_inputs"];
  cpu: z.infer<typeof resource>; gpu: z.infer<typeof resource>;
}) {
  const intent = conditions.parse(input.intent)
  if (!intent.target_parameters) throw Error("Explicit target parameters are required for a new plan")
  assertTargetBinding(intent.target, intent.site, intent.target_parameters)
  const cpu = resource.parse(input.cpu)
  const gpu = resource.parse(input.gpu)
  const selected = choose({ screening_mode: intent.screening_mode, requested_mode: intent.requested_mode,
    max_points: intent.max_points }, { cpu: cpu.points_per_hour, gpu: gpu.points_per_hour })
  if (selected.requires_confirmation) throw Error("Resource adjustment requires native confirmation before compilation")
  const concrete = selected.profile
  const durations = minutes(concrete)
  const created = Date.now()
  return planSchema.parse({ version: "screening-executor-v1", id: "screen-" + randomBytes(12).toString("hex"),
    request: input.source.text, origin: input.source.origin,
    directory: input.directory, session: input.session, target: intent.target, site: intent.site,
    target_parameters: intent.target_parameters,
    ...(input.research_inputs !== undefined ? { research_inputs: input.research_inputs } : {}),
    library: intent.library, scope: concrete.scope, max_records: concrete.max_records, shortlist: concrete.shortlist, seed: 20260725,
    assets: input.assets, runtime: input.runtime, connection: input.connection, delivery: defaultDelivery,
    automation: { version: plannerVersion, requested_mode: selected.requested, selected_mode: selected.selected,
      adjusted: selected.adjusted, intent_digest: digest(intent), estimated_points: selected.estimated_points,
      budget_headroom: intent.max_points === null ? null : Math.max(0, intent.max_points - selected.estimated_points),
      basis: "versioned-profile-plus-live-quota-rates" },
    steps: [
      { phase: "prepare", minutes: durations.prepare, resource: cpu },
      { phase: "carsidock", minutes: durations.carsidock, resource: gpu },
      { phase: "rtmscore", minutes: durations.rtmscore, resource: gpu },
      { phase: "report", minutes: durations.report, resource: cpu },
    ].map(step => ({ ...step, max_candidates: concrete.shortlist })),
    max_points: intent.max_points, gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 },
    created, approve_before: created + 30 * 60000, finish_before: created + 7 * 86400000 })
}
