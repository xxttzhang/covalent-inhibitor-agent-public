import { tool } from "@synsci/plugin/tool"
import { execute } from "./core"

const plugin = async () => ({
  tool: {
    inspire_screening: tool({
      description: "Inspire GPU covalent screening through the installed realZillionX/InspireSkill CLI. Use instead of Modal for the user's Inspire database workflow. Supports targets/preflight (read-only), plan (explicit candidates/minutes, live quota/image dry-run), start (saved plan_id with native human approval), status/logs/artifacts/cancel. Currently registers the prepared EGFR C797 pilot only; not whole-library preprocessing or AF3. No arbitrary shell, file paths, credentials, or self-approved flags. Plans expire after 30 minutes and remain bound to this session.",
      args: {
        action: tool.schema.enum(["targets", "preflight", "plan", "start", "status", "logs", "artifacts", "cancel"]),
        candidates: tool.schema.number().int().min(1).max(500).optional(),
        minutes: tool.schema.number().int().min(1).max(240).optional(),
        plan_id: tool.schema.string().optional(),
      },
      execute,
    }),
  },
  "experimental.chat.system.transform": async (_: unknown, output: { system: string[] }) => {
    output.system.push("Inspire integration: the user uses realZillionX/InspireSkill for remote GPU screening, not Modal. For this workflow use inspire_screening: targets/preflight, then plan with explicit candidate/runtime bounds, then start with the returned plan_id. Native approval is mandatory; no chat yes, boolean human_approved, or question form substitutes for that approval. Resource prices come from live Inspire quota in points/hour, not USD. Do not call external compute CLIs through bash or invent budgets. The supported initial scope is the existing prepared EGFR C797 pilot; other targets, full-library preprocessing, AF3 and downstream stages need separate audited integration. For known inhibitor records use the existing covindb_query MCP tool. A completed read/plan tool does not mean a GPU run completed.")
  },
})

export default plugin
