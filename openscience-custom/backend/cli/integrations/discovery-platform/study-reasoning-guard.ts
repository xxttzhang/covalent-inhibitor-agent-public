import { listStudies } from "./study-store"
import { readReasoning } from "./study-reasoning"

/** Validate model arguments against the host-owned session binding; never repair
 * provenance by silently inserting IDs into model output or saved artifacts. */
export async function guardStudyReasoning(directory: string, project: string,
  input: { tool: string; sessionID: string }, args: unknown) {
  if (!input.tool.endsWith("covalent_target_hypothesis")) return
  if (!args || typeof args !== "object" || !("action" in args) || args.action !== "finalize") return
  const studies = await listStudies(directory, project)
  for (const plan of studies) {
    const receipt = await readReasoning(directory, plan)
    if (receipt?.session !== input.sessionID) continue
    const hypothesis = "hypothesis" in args ? args.hypothesis : null
    if (!hypothesis || typeof hypothesis !== "object" || !("study_id" in hypothesis) || hypothesis.study_id !== plan.id)
      throw Error(`Finalize requires hypothesis.study_id=${plan.id} for this research session. Retry finalize with the same evidence_id and explicit study_id; do not collect again.`)
    if (!("route" in hypothesis) || typeof hypothesis.route !== "string"
      || !Object.entries(plan.routes).some(([route, value]) => route === hypothesis.route && value.selected))
      throw Error("Finalize route is not selected in this study; disabled routes are not blocked routes and must not be finalized.")
    return
  }
}
