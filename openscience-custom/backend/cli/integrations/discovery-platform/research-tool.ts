import { tool } from "@synsci/plugin/tool"
import { researchHostAction, type createResearchHost } from "./research-host"

export function researchTool(host: () => Promise<ReturnType<typeof createResearchHost>>, authorize: () => Promise<void>) {
  return tool({
    description: "Operate a registered, budgeted drug-design research loop in this conversation. list shows host-verified specifications; open freezes one existing request-bound specification and creates no computation. read shows actual evidence and remaining/reserved resources. tick advances one allowed step, using the real platform approval for the exact prepared native plan when needed; recover only restores existing evidence/actions. The project observer resumes results, scientific checks and subsequent bounded decisions automatically. Use the returned loop_id thereafter. Arguments cannot configure providers, tariffs, scientific paths, commands or approval. Execution success is not scientific acceptance, and unknown usage remains reserved.",
    args: researchHostAction.shape,
    async execute(args, context) {
      await authorize()
      return JSON.stringify(await (await host()).execute(args, context))
    },
  })
}
