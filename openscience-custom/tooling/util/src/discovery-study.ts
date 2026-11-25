import { z } from "zod"
import { warheadID } from "./warhead-registry"
import { discoveryRoute, projectPath } from "./discovery-config"
import { sha256, result } from "./discovery-result"
import type { CandidateHandoff } from "./candidate-handoff"

export const studyRoutes = ["library-first", "ligand-first", "de-novo"] as const
export const studyTarget = z.object({
  id: z.string().min(1), gene: z.string().min(1), taxon: z.number().int().positive(),
  uniprot: z.string().min(1), site: z.object({ chain: z.string().min(1), residue: z.string().min(1),
    number: z.number().int().positive(), atom: z.string().min(1), numbering: z.enum(["canonical", "structure"]) }).strict(),
  receptor: z.object({ reference: z.string().min(1),
    sha256: sha256.optional().describe("Omit when unknown. Never use an empty string or invented checksum.") }).strict(),
}).strict()
const inputs = z.object({ library: z.string().min(1).optional(), parent: z.string().min(1).optional(),
  parentStructure: z.string().min(1).optional(), receptorStructure: z.string().min(1).optional(),
  parentBinding: z.enum(["noncovalent", "covalent", "unknown"]).optional(),
  pocket: z.string().min(1).optional(), warhead: z.string().min(1).optional(),
}).strict()
const selection = z.object({ selected: z.boolean(), mode: z.enum(["reuse", "new"]), inputs,
  generation: z.object({ slots: z.number().int().positive(), seed: z.number().int().nonnegative() }).strict().optional(),
  planning: z.object({ screeningMode: z.enum(["economy", "standard", "deep", "test100", "research_pilot_v1"]),
    maxPoints: z.number().finite().nonnegative() }).strict().optional(),
  reason: z.string().min(1).optional() }).strict()
export const deNovoBranch = z.object({
  id: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/), warhead: warheadID,
  target: studyTarget.optional().describe("Explicit site/structure override; inherited parent identity otherwise. Requires its own verified evidence before execution."),
  pocket: z.string().min(1), generation: z.object({ slots: z.number().int().min(1).max(96), seed: z.number().int().nonnegative() }).strict(),
  rationale: z.string().min(1), evidence: z.array(z.object({ reference: z.string().min(1),
    interpretation: z.enum(["direct", "analogy", "hypothesis"]), statement: z.string().min(1) }).strict()).min(1),
}).strict()
export const studyRequest = z.object({
  denovoBranches: z.array(deNovoBranch).min(1).max(8).optional().describe("Research branches; declarations are not verified evidence or compute authorization."),
  title: z.string().min(1).max(300), target: studyTarget,
  hypothesis: z.object({ state: z.enum(["provided", "reused", "pending"]), rationale: z.string(),
    evidence: z.array(z.object({ reference: z.string().min(1),
      sha256: sha256.optional().describe("Omit unless the source bytes have actually been hashed.") }).strict()),
  }).strict(), routes: z.object({
    "library-first": selection.default({ selected: false, mode: "new", inputs: {} }),
    "ligand-first": selection.default({ selected: false, mode: "new", inputs: {} }),
    "de-novo": selection.default({ selected: false, mode: "new", inputs: {} }),
  }).strict().describe("Supply selected routes. Omitted routes are disabled, never executed or imported."),
}).strict().refine(value => studyRoutes.some(route => value.routes[route].selected), "Select at least one route")
  .refine(value => !value.denovoBranches || (value.routes["de-novo"].selected && value.routes["de-novo"].mode === "new"), "Branches require a new De novo route")
  .refine(value => !value.denovoBranches || new Set(value.denovoBranches.map(branch => branch.id)).size === value.denovoBranches.length, "Branch IDs must be unique")

export const study = studyRequest.safeExtend({
  denovoOrigin: z.object({ parent: z.string().regex(/^study-[a-f0-9-]{36}$/), parent_sha256: sha256,
    branch: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/) }).strict().optional(),
  schema: z.literal("discovery-study-v1"),
  id: z.string().regex(/^study-[a-f0-9-]{36}$/), project: z.string().min(1), createdAt: z.string().datetime(),
})
export type DiscoveryStudy = z.infer<typeof study>
export type StudyRequest = z.infer<typeof studyRequest>

export const importReceipt = z.object({ schema: z.literal("discovery-study-import-v1"), study: study.shape.id,
  project: z.string().min(1), target: z.string().min(1), route: discoveryRoute,
  importedAt: z.string().datetime(), mode: z.literal("historical-reuse"),
  targetIdentity: studyTarget.omit({ id: true, receptor: true }).optional(),
  source: z.object({ path: projectPath, sha256, project: z.string().min(1), run: z.string().min(1),
    evidenceUpdatedAt: z.string().datetime() }).strict(),
  snapshot: projectPath, candidates: z.number().int().nonnegative(),
  newComputeSubmitted: z.literal(false),
}).strict()
export type StudyImport = z.infer<typeof importReceipt>
export type StudyCandidateSource = { recordID: string; study: string; route: typeof studyRoutes[number];
  sourceRun: string; sourceSha256: string; mode: string; candidate: z.infer<typeof result>["candidates"][number];
  handoff?: CandidateHandoff }
export type StudyOverview = { schema: string; plan: DiscoveryStudy; newComputeSubmitted: boolean;
  libraryPlan?: { createdAt: string; planDigest: string; approved: false; submitted: false; evidence: string; plan: unknown } | null;
  denovoPlan?: { createdAt: string; planDigest: string; plan: unknown } | null;
  ligandPlan?: { createdAt: string; planDigest: string; plan: unknown; approved: false; submitted: false } | null;
  reasoning?: { study: string; project: string; session: string; createdAt: string; tool: string; computeAllowed: false } | null;
  hypothesisAttachments?: NonNullable<StudyOverview["hypothesisAttachment"]>[];
  hypothesisSources?: { source: string; sha256: string; route: string; state: string }[];
  reusableHypotheses?: { sourceStudy: string; title: string; route: string; sha256: string; source: string; receptor: string }[];
  hypothesisReuses?: { study: string; project: string; sourcePlan: DiscoveryStudy; source: string; sha256: string;
    original: string; route: string; importedAt: string; executionEligible: false }[];
  hypothesisAttachment?: { source: string; sha256: string; importedAt: string; original: string;
    contractValidation?: { state: string; blockers: string[]; compiled: boolean; submitted: boolean };
    executionBinding: string; value: { hypothesis: { route: string }; handoff: { state: string; [key: string]: unknown } } } | null;
  routes: { route: typeof studyRoutes[number]; state: string; missing: string[]; reason: string;
    imported: { receipt: StudyImport; data: z.infer<typeof result> } | null }[];
  candidates: StudyCandidateSource[];
  pool: { policy: string; rdkitVersion: string | null; ranked: boolean; sourceCount: number;
    groups: { id: string; identity: { state: string; canonical: string | null; reason: string }; sources: StudyCandidateSource[] }[] };
}

/** This is input readiness, not a claim that a scientific engine can execute. */
export function inputReadiness(value: StudyRequest, route: typeof studyRoutes[number]) {
  const selected = value.routes[route]
  if (!selected.selected) return { state: "not-selected" as const, missing: [], reason: selected.reason ?? "用户未选择此路线" }
  if (selected.mode === "reuse") return { state: "awaiting-import" as const, missing: [], reason: "等待校验并导入历史结果；不启动计算" }
  if (route === "de-novo" && value.denovoBranches) return { state: "awaiting-adapter-validation" as const,
    missing: [], reason: "多战头父研究：逐分支检查证据和能力，每个子研究独立冻结计划与审批；父研究不提交计算" }
  const missing = [!selected.inputs.warhead && "warhead",
    route === "library-first" && !selected.inputs.library && "library",
    route === "library-first" && !selected.planning && "screening_scale_and_budget",
    route === "ligand-first" && !selected.inputs.parent && "parent",
    route === "ligand-first" && !selected.inputs.parentStructure && "parentStructure",
    route === "ligand-first" && !selected.inputs.receptorStructure && "receptorStructure",
    route === "ligand-first" && selected.inputs.parentBinding !== "noncovalent" && "noncovalent_parent_evidence",
    route === "de-novo" && !selected.inputs.pocket && "pocket",
    route === "de-novo" && !selected.generation && "generation_slots_and_seed",
  ].filter((item): item is string => typeof item === "string")
  return { state: missing.length ? "missing-inputs" as const : "awaiting-adapter-validation" as const,
    missing, reason: missing.length ? "请补齐路线输入" : "输入已声明；仍须实际路线适配器校验和编译，尚未提交计算" }
}

export function importedResult(input: unknown, plan: DiscoveryStudy, route: typeof studyRoutes[number]) {
  const data = result.parse(input)
  if (!plan.routes[route].selected || plan.routes[route].mode !== "reuse") throw Error("Route is not selected for historical reuse")
  if (data.project !== plan.project || data.target !== plan.target.id || data.route !== route) throw Error("Historical result project/target/route mismatch")
  if (!data.sources.length) throw Error("Historical result has no provenance sources")
  return data
}
