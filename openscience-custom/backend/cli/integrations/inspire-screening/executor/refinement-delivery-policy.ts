import z from "zod"
import { rankingCode } from './refinement-postprocess-code'

export const refinementDeliveryPolicy = z.object({ mode: z.literal("all-refinement-artifacts-v1"),
  max_file_bytes: z.number().int().positive().max(128 * 1024 * 1024),
  max_total_bytes: z.number().int().positive().max(1024 * 1024 * 1024),
  max_files: z.number().int().positive().max(2000),
  final_ranking: z.enum(['af3-strain-pareto-v1', 'library-first-pareto-v2.0']).optional(),
  candidate_results: z.object({ mode: z.literal('candidate-results-v1'), code_sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict().optional(),
}).strict().refine((value) => value.max_file_bytes <= value.max_total_bytes)
  .refine((value) => (value.final_ranking === 'library-first-pareto-v2.0') === !!value.candidate_results,
    'Full Pareto requires the approved candidate-results postprocessor')

// Applied only by the new-plan compiler; never defaulted onto historical grants.
export const defaultRefinementDelivery = () => refinementDeliveryPolicy.parse({ mode: "all-refinement-artifacts-v1",
  final_ranking: 'library-first-pareto-v2.0',
  candidate_results: { mode: 'candidate-results-v1', code_sha256: rankingCode() },
  max_file_bytes: 128 * 1024 * 1024, max_total_bytes: 1024 * 1024 * 1024, max_files: 2000 })
