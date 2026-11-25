import z from "zod"
import { targetParameters } from "@synsci/util/screening-target"
import { screeningMode } from "./resource-planner"

/** Shared input contract only: parsing never opens stores, approvals or remote resources. */
export const createFields = {
  request: z.string().min(1).max(10000).optional(),
  target: targetParameters.shape.target.optional(), site: z.string().min(1).max(32).optional(),
  target_parameters: targetParameters.optional().describe("Explicit target/receptor/chain/site/numbering/reaction; no default target."),
  library: z.literal("CovInDB/Virtual_Screening_Library.csv").optional().describe("Explicit virtual library; never substitute known inhibitors or a historical pilot."),
  screening_mode: screeningMode.optional().describe("Explicit scientific scale; exact resources are host-planned and require native approval."),
  max_points: z.number().finite().nonnegative().nullable().optional().describe("Explicit points cap; null only for an explicitly requested test100 no-cap test. Omission is not consent."),
}
export const createContract = z.object({ action: z.literal("create"), ...createFields,
  request: z.string().min(1).max(10000) }).strict()
