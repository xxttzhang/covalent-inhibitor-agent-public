import { createHash } from "node:crypto"
import { jsonSchema } from "ai"
import z from "zod"
import { Lock } from "../util/lock"
import { boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { readDeNovoPlan } from "../../integrations/discovery-platform/study-denovo-plan"
import { planDeNovoBatch, statusDeNovoBatch } from "../tool/denovo-selection"
import type { RecordTools } from "./route-workflow"

export function boundBatchSubmission(value: unknown, task: string, stage: "vs" | "lo" | "glide" = "vs") {
  const workflow = "de-flow-" + createHash("sha256").update(task).digest("hex").slice(0, 24)
  const plan = z.object({ schema: z.literal(`denovo-batch-${stage}-plan-v1`),
    workflow_id: z.literal(workflow), manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    compute_submitted: z.literal(false), native_approval_required: z.literal(true),
    submit_tool: z.literal("covalent_submit_job"), submit_arguments: z.object({
      request_id: z.string(), project_id: z.literal(workflow), route: z.literal("de-novo"),
      engine: z.literal(stage === "glide" ? "de-novo-prereaction-glide" : `schrodinger-covdock-${stage}`), execution_mode: z.literal("execute"),
      human_approved: z.literal(false), approval_note: z.null(), attempt: stage === "lo" ? z.union([z.literal(1), z.literal(2)]) : z.literal(1), parent_run_id: stage === "lo" ? z.string().nullable() : z.null(),
      parameters: stage === "vs" ? z.object({ vs_preparation: z.record(z.string(), z.unknown()) }).strict()
        : stage === "glide" ? z.object({ workflow_id: z.literal(workflow), dispatch_policy: z.literal("denovo-prereaction-glide-instance-v1"),
            execution_instance: z.literal("asgbie3"), prereaction_source_sha256: z.string().regex(/^[a-f0-9]{64}$/),
            prereaction_manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
            prepared_candidate_ids: z.array(z.string().startsWith("PXM_")).min(1).max(5),
            control_parent: z.literal("CONTROL_AFATINIB"), max_control_states: z.literal(2), input_directory: z.string() }).strict()
        : z.object({ workflow_id: z.literal(workflow), dispatch_policy: z.literal("denovo-batch-lo-instance-v1"),
            execution_instance: z.literal("asgbie3"), denovo_batch_lo_sha256: z.string() }).passthrough(),
      resource_budget: z.object({ cpu_count: z.number().int().min(1).max(stage === "vs" ? 55 : stage === "lo" ? 12 : 4), gpu_count: z.literal(0), memory_gb: z.number().int().min(8).max(stage === "vs" ? 220 : 64),
        max_runtime_minutes: z.literal(stage === "vs" ? 60 : stage === "lo" ? 240 : 120),
        max_candidates: z.number().int().min(stage === "glide" ? 3 : 1).max(stage === "vs" ? 100 : stage === "lo" ? 12 : 7) }).strict(),
    }).strict(),
  }).passthrough().parse(value)
  if ((plan.submit_arguments.attempt === 1 && plan.submit_arguments.parent_run_id !== null)
    || (plan.submit_arguments.attempt === 2 && (stage !== "lo" || !plan.submit_arguments.parent_run_id)))
    throw Error("Batch retry ancestry is missing or changed")
  if (plan.submit_arguments.request_id !== workflow + `-${stage === "glide" ? "prereaction" : stage}-` + plan.manifest_sha256.slice(0, 12)
    || (stage === "vs" ? plan.submit_arguments.resource_budget.cpu_count > plan.submit_arguments.resource_budget.max_candidates
      : plan.submit_arguments.resource_budget.cpu_count !== (stage === "glide" ? 4 : Math.min(12, plan.submit_arguments.resource_budget.max_candidates)))
    || plan.submit_arguments.resource_budget.memory_gb !== (stage === "glide" ? 16 : Math.max(stage === "vs" ? 16 : 8, 4 * plan.submit_arguments.resource_budget.cpu_count))
    || (stage === "lo" && (!("denovo_batch_lo_sha256" in plan.submit_arguments.parameters)
      || plan.submit_arguments.parameters.denovo_batch_lo_sha256 !== plan.manifest_sha256)))
    throw Error("Batch request differs from its frozen input")
  if (stage === "glide") {
    const p = z.object({ prereaction_manifest_sha256: z.literal(plan.manifest_sha256),
      prereaction_source_sha256: z.string(), prepared_candidate_ids: z.array(z.string()) }).passthrough().parse(plan.submit_arguments.parameters)
    if (p.prereaction_source_sha256 !== plan.source_sha256 || new Set(p.prepared_candidate_ids).size !== p.prepared_candidate_ids.length
      || plan.submit_arguments.resource_budget.max_candidates !== p.prepared_candidate_ids.length + 2)
      throw Error("Glide candidate/control denominator or source changed")
  }
  return plan.submit_arguments
}

export function deNovoBatchSubmissionTool(tools: RecordTools,
  scope: { project: string; session: string; directory: string }, studyID: string,
  available: () => Promise<boolean>, stage: "vs" | "lo" | "glide" = "vs") {
  const execute = tools["covalent-discovery_covalent_submit_job"]?.execute
  if (!execute) return undefined
  return {
    description: `Submit the bound PocketXMol batch's frozen ${stage.toUpperCase()} plan through native approval. The host supplies task identity, selected candidates and budget. No model-supplied job parameters or approval. Never submits MD. After uncertainty inspect the existing request; never create a replacement.`,
    inputSchema: jsonSchema({ type: "object", properties: {}, additionalProperties: false }),
    execute: async (args: unknown, options: Parameters<typeof execute>[1]) => {
      z.object({}).strict().parse(args)
      using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
      if (!await available()) throw Error("Another research route is active")
      const study = await boundSessionStudy(scope)
      if (!study || study.id !== studyID || !study.routes["de-novo"].selected
        || study.routes["de-novo"].mode !== "new" || study.routes["library-first"].selected
        || study.routes["ligand-first"].selected) throw Error("Native De novo study binding changed")
      const saved = await readDeNovoPlan(scope.directory, study)
      if (!saved) throw Error("Missing frozen generation plan")
      const task = String(saved.plan.request.id)
      const plan = stage === "vs" ? await planDeNovoBatch(task, scope.directory, scope.session)
        : await statusDeNovoBatch(task, scope.directory, scope.session, stage === "lo" ? "plan_lo" : "plan_prereaction_batch")
      const request = boundBatchSubmission(plan, task, stage)
      const { registerDeNovoCompute } = await import("./denovo-continuation-compute")
      await registerDeNovoCompute(scope, study.id, stage, task, request.request_id)
      return await execute(request, options)
    },
  }
}
