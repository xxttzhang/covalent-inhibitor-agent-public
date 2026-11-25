import z from "zod"
import { createHash } from "node:crypto"

export const researchHash = z.string().regex(/^[a-f0-9]{64}$/)
export const researchResources = z.object({
  gpu_hours: z.number().finite().nonnegative(),
  cpu_core_hours: z.number().finite().nonnegative(),
  compute_points: z.number().finite().nonnegative(),
  model_tokens: z.number().int().nonnegative(),
  model_cost: z.number().finite().nonnegative(),
}).strict()
/** null is an explicitly uncapped monetary limit, never a zero price. */
export const exceedsResearchLimit = (amount: number, limit: number | null) => limit !== null && amount > limit + 1e-9
export type ResearchResources = z.infer<typeof researchResources>
export const emptyResources = (): ResearchResources => ({ gpu_hours: 0, cpu_core_hours: 0,
  compute_points: 0, model_tokens: 0, model_cost: 0 })
export const resourceKeys = Object.keys(emptyResources()) as (keyof ResearchResources)[]

export const researchContract = z.object({
  schema: z.literal("drug-design-research-contract-v1"),
  directory: z.string().startsWith("/"), project: z.string().min(1), session: z.string().min(1),
  objective: z.string().min(1), mode: z.enum(["offline-replay", "native-compute"]),
  route: z.literal("library-first"),
  target: z.object({ gene: z.literal("EGFR"), taxon: z.literal(9606), uniprot: z.literal("P00533"),
    canonical_site: z.literal("C797"), pdb: z.literal("1M17"), chain: z.literal("A"),
    author_residue: z.literal(773), reactive_atom: z.literal("SG"), warhead: z.literal("acrylamide"),
    receptor_sha256: researchHash.nullable(), mapping_sha256: researchHash.nullable() }).strict(),
  inputs: z.object({ library: z.string().min(1), library_sha256: researchHash.nullable(),
    subset_sha256: researchHash.nullable(), source_release: researchHash, science_config_sha256: researchHash.nullable() }).strict(),
  quality: z.object({ manifest_sha256: researchHash.nullable(), required_checks: z.array(z.string().min(1)).min(1),
    minimum_candidates: z.number().int().min(1).max(5), conclusion: z.literal("computational-shortlist-only") }).strict(),
  limits: z.object({ resources: researchResources.extend({ model_cost: z.number().finite().nonnegative().nullable() }), max_library_records: z.literal(20000),
    max_rounds: z.number().int().min(1).max(2), max_candidates_per_round: z.number().int().min(1).max(5),
    max_total_candidates: z.number().int().min(1).max(10), max_gpu_concurrency: z.literal(1),
    max_gpu_hours_per_round: z.number().positive().max(0.25), max_decisions: z.number().int().positive(),
    max_elapsed_ms: z.number().int().positive() }).strict(),
  allowed_adaptations: z.array(z.enum(["inspect", "evaluate", "next_frozen_batch", "stop"])).min(1),
}).strict().superRefine((value, ctx) => {
  if (value.mode === "native-compute") {
    for (const [group, fields] of Object.entries({
      target: { receptor_sha256: value.target.receptor_sha256, mapping_sha256: value.target.mapping_sha256 },
      inputs: { library_sha256: value.inputs.library_sha256, subset_sha256: value.inputs.subset_sha256,
        science_config_sha256: value.inputs.science_config_sha256 },
      quality: { manifest_sha256: value.quality.manifest_sha256 },
    })) for (const [field, hash] of Object.entries(fields))
      if (hash === null) ctx.addIssue({ code: "custom", path: [group, field], message: "Native computation requires a verified scientific binding hash" })
  }
  if (value.limits.resources.gpu_hours > 0.5)
    ctx.addIssue({ code: "custom", message: "Pilot total GPU budget exceeds 0.5 GPU hours" })
  if (value.limits.max_total_candidates > value.limits.max_rounds * value.limits.max_candidates_per_round)
    ctx.addIssue({ code: "custom", message: "Candidate total exceeds declared round capacity" })
})
export type ResearchContract = z.infer<typeof researchContract>
export const researchState = z.enum(["READY", "DECIDING", "PREPARING", "AWAITING_APPROVAL", "RUNNING", "EVALUATING", "BLOCKED", "FINISHED", "CANCELLED"])
export type ResearchState = z.infer<typeof researchState>
export const researchOutcome = z.enum(["objective_met", "no_viable_candidate", "insufficient_evidence", "budget_exhausted", "capability_unavailable", "integrity_failure", "user_cancelled", "approval_declined"])
export type ResearchOutcome = z.infer<typeof researchOutcome>

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value !== null && typeof value === "object")
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]))
  return value
}
export const researchDigest = (value: unknown) => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex")

export const researchReservation = z.object({
  key: z.string().min(1), kind: z.enum(["model", "compute", "evaluation"]), resources: researchResources,
  round: z.number().int().min(0).max(2), candidate_slots: z.number().int().min(0).max(5),
  plan_digest: researchHash, source_release: researchHash,
}).strict().superRefine((value, ctx) => {
  if (value.kind === "model" && (value.round !== 0 || value.candidate_slots !== 0 || value.resources.gpu_hours !== 0 || value.resources.cpu_core_hours !== 0 || value.resources.compute_points !== 0))
    ctx.addIssue({ code: "custom", message: "Model reservations cannot request scientific computation" })
  if (value.kind === "compute" && (value.round < 1 || value.candidate_slots < 1 || value.resources.model_tokens !== 0 || value.resources.model_cost !== 0))
    ctx.addIssue({ code: "custom", message: "Compute reservation requires a bounded candidate round" })
  if (value.kind === "evaluation" && (value.round !== 0 || value.candidate_slots !== 0 || value.resources.cpu_core_hours <= 0
    || value.resources.gpu_hours !== 0 || value.resources.compute_points !== 0
    || value.resources.model_tokens !== 0 || value.resources.model_cost !== 0))
    ctx.addIssue({ code: "custom", message: "Scientific evaluation reserves only positive local CPU usage, without candidate rounds or model calls" })
})
export type ResearchReservation = z.infer<typeof researchReservation>
