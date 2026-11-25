import { tool } from "@synsci/plugin/tool"
import { studyRequest, study } from "@synsci/util/discovery-study"
import { studyActionResult } from "./study-actions"
import { bindStudySession, sessionStudy, createSessionStudy } from "./study-session"

export function studyTool(directory: string, project: string, authorize: () => Promise<void>, localChemistry: () => Promise<boolean>, authorizeRemote?: () => Promise<void>) {
  return tool({
    description: "Create and inspect a unified research study. For multi-warhead De novo, declare denovoBranches with stable IDs, exact warhead IDs from denovo_generate.warheads, pocket, slots/seed and evidence rationale; optional branch target overrides require their own evidence. Use denovo_generate.branches and branch_study to inspect/materialize independent child studies. Declared citations are not verified hypotheses. Create and inspect a unified research study, or import registered historical Library-first/Ligand-first/De novo results. This tool never submits scientific computation. Each new study has a unique ID; reuse is explicitly labeled and snapshots preserve source run, timestamps and hashes. Do not describe imported history as a newly completed run. New-mode inputs require subsequent real adapter validation; this tool does not certify execution readiness or approve resources. Use create with request, then import separately for selected reuse routes. get returns all route statuses and source-preserving candidate records. Never substitute the currently registered target for a different requested target.",
    args: { action: tool.schema.enum(["create", "list", "get", "import", "attach-hypothesis", "sync-hypotheses", "reuse-hypothesis", "start-reasoning", "reasoning-status", "resume-reasoning", "compile-library", "compile-denovo", "bind-ligand-plan"]),
      source: tool.schema.string().optional(),
      source_study: study.shape.id.optional(),
      sha256: tool.schema.string().regex(/^[a-f0-9]{64}$/).optional(),
      request: studyRequest.optional(), study_id: study.shape.id.optional().describe(
        "Omit to use the study bound by a successful create/get in this session. Explicit IDs are never corrected or guessed."),
      route: tool.schema.enum(["library-first", "ligand-first", "de-novo"]).optional() },
    async execute(args, context) {
      await authorize()
      const scope = { directory, project, session: context.sessionID }
      if (args.action === "create") {
        const created = await createSessionStudy(scope, args.request)
        return JSON.stringify({ study_id: created.plan.id, ...created, newComputeSubmitted: false,
          next: created.plan.routes["de-novo"].selected && created.plan.routes["de-novo"].mode === "new"
            ? { tool: "denovo_generate", action: "workflow" }
            : { tool: "discovery_study", action: "get" } })
      }
      const resolved = ["list", "create"].includes(args.action) ? args
        : { ...args, study_id: await sessionStudy(scope, args.study_id) }
      const result = await studyActionResult(directory, project, resolved, await localChemistry(), authorizeRemote, context.sessionID)
      if (args.action === "reasoning-status") {
        const { acknowledgeDeNovoReasoning } = await import("../../src/session/denovo-continuation-runtime")
        await acknowledgeDeNovoReasoning(scope, result)
      }
      if (args.action === "get")
        await bindStudySession(scope, study.shape.id.parse(resolved.study_id))
      return JSON.stringify(result)
    },
  })
}
