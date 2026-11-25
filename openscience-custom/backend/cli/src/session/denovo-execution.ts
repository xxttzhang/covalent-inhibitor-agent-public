import { jsonSchema } from "ai"
import z from "zod"
import { Lock } from "../util/lock"
import { readStudy } from "../../integrations/discovery-platform/study-store"
import { boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { readDeNovoIndependentInput, syncDeNovoIndependentReview } from "../../integrations/discovery-platform/study-denovo-review"
import type { RecordTools } from "./route-workflow"

const stage = z.enum(["prereaction", "adduct"])
const selection = z.object({ stage }).strict()

/** The model selects a stage, never a raw job, source, budget or approval. */
export function boundDeNovoSubmission(plan: Record<string, unknown>, study: { id: string; project: string },
  input: { source_id: string; source_manifest_sha256: string }, selected: z.infer<typeof stage>) {
  const request = z.object({ request_id: z.string(), project_id: z.literal(study.project),
    route: z.literal("de-novo"), engine: z.string(), execution_mode: z.literal("execute"),
    parameters: z.record(z.string(), z.unknown()),
    resource_budget: z.object({ cpu_count: z.literal(1), gpu_count: z.literal(0), memory_gb: z.literal(8),
      max_runtime_minutes: z.literal(30), max_candidates: z.literal(2) }).strict(),
    human_approved: z.literal(false), approval_note: z.null(), attempt: z.union([z.literal(1), z.literal(2)]), parent_run_id: z.string().nullable(),
  }).strict().parse(plan.submit_arguments)
  const suffix = selected === "prereaction" ? "prereaction" : "covdock"
  const engine = selected === "prereaction" ? "de-novo-prereaction-vina" : "schrodinger-covdock-lo"
  const recovery = request.attempt === 2
  if (recovery) {
    const lineage = z.object({ protocol: z.literal("denovo-undispatched-cancel-v1"), parent_run_id: z.string(),
      parent_spec_sha256: z.string().regex(/^[a-f0-9]{64}$/), parent_state: z.literal("CANCELLED"),
      compute_started: z.literal(false), candidate_and_control_unchanged: z.literal(true) }).strict().parse(plan.recovery)
    if (selected !== "adduct" || request.parent_run_id !== lineage.parent_run_id
      || request.parameters.dispatch_policy !== "denovo-independent-local-v1") throw Error("Invalid De novo recovery lineage")
  }
  if (!recovery && request.parent_run_id !== null) throw Error("Initial evaluation cannot have a parent")
  if (plan.study_id !== study.id || plan.source_id !== input.source_id
    || plan.source_manifest_sha256 !== input.source_manifest_sha256 || plan.compute_submitted !== false
    || plan.native_approval_required !== true || plan.submit_tool !== "covalent_submit_job"
    || request.request_id !== `${input.source_id}-${suffix}${recovery ? "-retry-2" : ""}` || request.engine !== engine
    || request.parameters.workflow_id !== study.id || request.parameters.denovo_source_id !== input.source_id
    || request.parameters.denovo_source_manifest_sha256 !== input.source_manifest_sha256)
    throw Error("Independent submission differs from this study's frozen source and stage")
  return request
}

export function boundDeNovoCancellation(plan: Record<string, unknown>, study: { id: string; project: string },
  input: { source_id: string; source_manifest_sha256: string }) {
  const request = z.object({ run_id: z.string(), human_approved: z.literal(true) }).strict().parse(plan.cancel_arguments)
  if (plan.study_id !== study.id || plan.project_id !== study.project || plan.source_id !== input.source_id
    || plan.source_manifest_sha256 !== input.source_manifest_sha256 || plan.compute_submitted !== false
    || plan.state !== "QUEUED" || plan.run_id !== request.run_id || plan.cancel_tool !== "covalent_cancel_job"
    || plan.native_approval_required !== true) throw Error("Cancellation differs from the bound undispatched attempt")
  return request
}

export function deNovoCancellationTool(tools: RecordTools, scope: { project: string; session: string; directory: string },
  studyID: string, available: () => Promise<boolean>) {
  const cancel = tools["covalent-discovery_covalent_cancel_job"]
  if (!cancel?.execute) return undefined
  const execute = cancel.execute
  return {
    description: "Cancel only this study's verified never-dispatched CovDock attempt through exact native approval. No process termination is inferred. The host chooses the existing run; model-supplied job IDs are forbidden. Inspect status after any uncertainty. Does not replan or submit compute.",
    inputSchema: jsonSchema({ type: "object", properties: { stage: { type: "string", enum: ["adduct"] } },
      required: ["stage"], additionalProperties: false }),
    execute: async (args: unknown, options: Parameters<typeof execute>[1]) => {
      z.object({ stage: z.literal("adduct") }).strict().parse(args)
      using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
      if (!await available()) throw Error("Another research route is active")
      const current = await boundSessionStudy(scope)
      if (!current || current.id !== studyID || !current.routes["de-novo"].selected
        || current.routes["de-novo"].mode !== "new" || current.routes["library-first"].selected
        || current.routes["ligand-first"].selected) throw Error("Native De novo study binding changed")
      const study = await readStudy(scope.directory, scope.project, studyID)
      const input = await readDeNovoIndependentInput(scope.directory, study)
      if (!input) throw Error("Missing frozen independent source")
      const plan = await syncDeNovoIndependentReview(scope.directory, study, "cancel_plan")
      return await execute(boundDeNovoCancellation(plan, study, input), options)
    },
  }
}

/** Receives the already approval-guarded MCP tool from the session resolver. */
export function deNovoSubmissionTool(tools: RecordTools, scope: { project: string; session: string; directory: string },
  studyID: string, available: () => Promise<boolean>) {
  const submit = tools["covalent-discovery_covalent_submit_job"]
  if (!submit?.execute) return undefined
  const execute = submit.execute
  return {
    description: "Submit the current native De novo study's frozen independent evaluation through its exact native remote-compute approval. Choose prereaction (Vina) or adduct (CovDock). The host binds the candidate, source, request and budget; raw job parameters and approval receipts are forbidden. Does not generate candidates or run MD. On errors or timeouts, stop and inspect the existing request through denovo_generate status actions before considering any retry.",
    inputSchema: jsonSchema({ type: "object", properties: { stage: { type: "string", enum: ["prereaction", "adduct"] } },
      required: ["stage"], additionalProperties: false }),
    execute: async (args: unknown, options: Parameters<typeof execute>[1]) => {
      const selected = selection.parse(args).stage
      using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
      if (!await available()) throw Error("Another research route is active; use its bound execution tools")
      const current = await boundSessionStudy(scope)
      if (!current || current.id !== studyID || !current.routes["de-novo"].selected
        || current.routes["de-novo"].mode !== "new" || current.routes["library-first"].selected
        || current.routes["ligand-first"].selected) throw Error("Native De novo study binding changed")
      const study = await readStudy(scope.directory, scope.project, studyID)
      const input = await readDeNovoIndependentInput(scope.directory, study)
      if (!input) throw Error("Freeze and register one native-study independent input before submission")
      const plan = await syncDeNovoIndependentReview(scope.directory, study, selected === "prereaction" ? "plan_prereaction" : "plan")
      const request = boundDeNovoSubmission(plan, study, input, selected)
      // The guarded executor prepares the backend plan, shows the one-time
      // permission card, verifies its durable reply and then signs the receipt.
      return await execute(request, options)
    },
  }
}
