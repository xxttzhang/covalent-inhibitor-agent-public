import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { createContract } from "../inspire-screening/executor/create-contract"
import { targetAdmission } from "../inspire-screening/executor/target"
import { studyLigandContract } from "./study-ligand-contract"
import { studyLibraryAdmission } from "./study-admission"

/** Validate against the actual executor contract. Never calls create, approves, or submits. */
export function studyExecutionContract(plan: DiscoveryStudy, original: string) {
  const proposal = z.object({ hypothesis: z.object({ route: z.string() }), handoff: z.object({
    next_call: z.object({ tool: z.string(), arguments: z.unknown() }).nullable().optional(),
  }).passthrough() }).parse(JSON.parse(original))
  const call = proposal.handoff.next_call
  if (proposal.hypothesis.route === "ligand-first") return studyLigandContract(plan, original)
  if (proposal.hypothesis.route !== "library-first" || call?.tool !== "inspire_screening_task") {
    return { state: "NOT_VALIDATED", blockers: ["This route requires its own compiler binding; no fallback to Library-first"], compiled: false, submitted: false }
  }
  const parsed = createContract.safeParse(call.arguments)
  if (!parsed.success) return { state: "CONTRACT_REJECTED", blockers: parsed.error.issues.map(item => item.path.join(".") + ": " + item.message), compiled: false, submitted: false }
  const declared = plan.routes["library-first"].planning
  const args = { ...parsed.data,
    screening_mode: parsed.data.screening_mode ?? declared?.screeningMode,
    max_points: parsed.data.max_points === undefined ? declared?.maxPoints : parsed.data.max_points }
  const p = args.target_parameters
  const target = plan.target
  const inputs = plan.routes["library-first"].inputs
  const blockers: string[] = []
  // Use the same installed-adapter gate as the page. A direct compile call
  // must not accept a species/protein combination that the UI marks unsupported.
  const admission = studyLibraryAdmission(plan)
  if (!admission.supported) blockers.push(admission.reason)
  if (!p) blockers.push("Missing explicit target parameters")
  if (p && (args.target !== target.gene || p.target !== target.gene || args.site !== "C" + target.site.number
    || p.chain !== target.site.chain || p.residue !== target.site.residue.toUpperCase().replace(/^C$/, "CYS")
    || p.residue_number !== target.site.number || p.numbering !== target.site.numbering
    || p.receptor !== target.receptor.reference.replace(/^PDB:/i, "")
    || p.receptor_sha256 !== target.receptor.sha256 || target.site.atom !== "SG")) blockers.push("Study receptor/chain/site differs from proposed executor parameters")
  if (p) {
    const admission = targetAdmission(p)
    if (!admission.supported) blockers.push(admission.reason)
  }
  if (!inputs.library) blockers.push("Study has no explicit library input; proposal library is not user-confirmed")
  else if (inputs.library !== args.library) blockers.push("Study library differs from proposal")
  if (inputs.warhead !== "acrylamide") blockers.push("Study warhead missing or not supported by this adapter")
  if (!declared) blockers.push("Study must explicitly declare screening scale and resource budget; proposal values are not user confirmation")
  if (declared && (args.screening_mode !== declared.screeningMode || args.max_points !== declared.maxPoints))
    blockers.push("Proposal scale or budget differs from the immutable study plan")
  return { state: blockers.length ? "NEEDS_INPUTS_OR_ADAPTER" : "CONTRACT_VALIDATED_NOT_COMPILED", blockers,
    parameters: args, compiled: false, submitted: false }
}
