import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { conditions } from "../inspire-screening/executor/intake"
import { compileScreening } from "../inspire-screening/executor/compile"
import { studyExecutionContract } from "./study-execution-contract"

type Environment = Omit<Parameters<typeof compileScreening>[0], "intent">

/** Bind the immutable study and upstream proposal to the real executor compiler.
 * Environment is supplied by the host, never by the model or study form.
 * This function does not store a task, grant approval or submit computation. */
export function compileStudyLibrary(study: DiscoveryStudy, original: string, environment: Environment) {
  const route = study.routes["library-first"]
  if (!route.selected || route.mode !== "new") throw Error("Only an explicitly selected new Library-first plan may be compiled")
  const proposal = z.object({ hypothesis: z.object({ study_id: z.string(), route: z.literal("library-first") }),
    target: z.object({ symbol: z.string(), site: z.string(), taxon: z.number(), uniprot_id: z.string() }) }).parse(JSON.parse(original))
  if (proposal.hypothesis.study_id !== study.id || proposal.target.symbol !== study.target.gene
    || proposal.target.uniprot_id !== study.target.uniprot || proposal.target.taxon !== study.target.taxon
    || proposal.target.site !== "C" + study.target.site.number) throw Error("Upstream proposal does not belong to this study target")
  const contract = studyExecutionContract(study, original)
  if (contract.state !== "CONTRACT_VALIDATED_NOT_COMPILED" || !contract.parameters)
    return { state: contract.state, blockers: contract.blockers, compiled: false as const, submitted: false as const }
  const args = contract.parameters
  const intent = conditions.parse({ target: args.target, site: args.site, target_parameters: args.target_parameters,
    library: args.library, screening_mode: args.screening_mode, max_points: args.max_points })
  const plan = compileScreening({ ...environment, intent })
  return { state: "PLAN_COMPILED_NOT_APPROVED", study: study.id, route: "library-first",
    plan, blockers: [], compiled: true as const, submitted: false as const,
    note: "Host environment evidence still requires live verification; compilation is neither approval nor scientific execution." }
}
