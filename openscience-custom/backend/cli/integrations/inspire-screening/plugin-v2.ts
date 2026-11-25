import { tool } from "@synsci/plugin/tool"
import { execute as pilot } from "./core"
import { execute, schema } from "./workflow"
import type { ToolContext } from "@synsci/plugin/tool"

// A deliberately separate name and explicit purpose prevent an old test set
// from being mistaken for the user's current research workflow.
export default async () => ({
  tool: {
    inspire_library_workflow: tool({
      description: "Default entry for the user's Library-first workflow using realZillionX/InspireSkill: target + CovInDB VIRTUAL SCREENING library → preparation/QC → CarsiDock → RTMScore → evidence-based small AF3/CovDock subsets. describe shows protocol; inspect performs real read-only remote asset inspection; draft saves a NON-EXECUTABLE local scoped plan with missing inputs. Full-chain dispatch is not validated and is never implied. No legacy pilot/130-molecule reuse, no automatic all-library compute, no approval booleans.",
      args: schema.shape,
      execute,
    }),
    inspire_screening_smoke: tool({
      description: "CONNECTION TEST ONLY. Uses an old prepared EGFR pilot and takes its first N rows, NOT the formal virtual-library workflow and NOT a newly selected shortlist. Never use to fulfill a research screening request. Requires explicit user request for this historical-pilot connection test. Native exact-plan approval still required for GPU submission.",
      args: {
        action: tool.schema.enum(["targets", "preflight", "plan", "start", "status", "logs", "artifacts", "cancel"]),
        purpose: tool.schema.literal("historical_pilot_connection_test"),
        candidates: tool.schema.number().int().min(1).max(500).optional(),
        minutes: tool.schema.number().int().min(1).max(240).optional(),
        plan_id: tool.schema.string().optional(),
      },
      execute: async (input, context: ToolContext) => {
        const { purpose, ...args } = input
        if (purpose !== "historical_pilot_connection_test") throw new Error("Not a research screening entry point")
        return pilot(args, context)
      },
    }),
  },
  "experimental.chat.system.transform": async (_: unknown, output: { system: string[] }) => {
    output.system.push("User-confirmed workflow: Library-first from CovInDB VIRTUAL SCREENING LIBRARY. Use inspire_library_workflow by default. Explain: establish target/site and library → prepare/QC → CarsiDock-Cov primary screen → RTMScore on existing poses → choose a small, explicit uncertainty/diversity subset → AF3 and licensed CovDock-LO refinement → auditable summary. Use realZillionX/InspireSkill for remote access, not Modal. The user's current target/site are recorded in the workflow configuration; never change them silently. The known-inhibitor evidence table is a reference, not the candidate library. Do not substitute the historical 130-molecule pilot or claim first N rows are newly screened best candidates. Call inspect/draft to expose missing scope, candidate budgets, receptor/site audit, input hashes, thresholds, resources, licenses and runtime verification. DRAFT_NOT_EXECUTABLE means no execution is possible through this tool; do not route around it with bash, legacy submit tools or smoke. Each future stage needs a validated parent artifact, failure denominator, explicit promoted IDs, exact resource approval and no automatic retries. Existing project route conflicts require an isolated new campaign, not overwriting its route. Asset availability, plans, job submissions and successful scientific outcomes are distinct. No current full-chain GPU validation has been performed. AF3 mPAE is placement confidence, not affinity; never compare raw heterogeneous scores directly. inspire_screening_smoke is only for a separately requested historical-pilot connection test, never the current formal research request.")
  },
})
