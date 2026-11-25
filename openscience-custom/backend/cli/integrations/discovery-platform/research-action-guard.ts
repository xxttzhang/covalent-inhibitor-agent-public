import { researchContract, resourceKeys, type ResearchContract, type ResearchResources } from "./research-loop-contract"
import { parseResearchDecision, type ResearchDecisionContext } from "./research-decision"
import { verifyResearchObservation, type ResearchObservation } from "./research-observations"

export type ResearchGuardState = {
  contract: ResearchContract
  context: ResearchDecisionContext
  observations: readonly ResearchObservation[]
  resources: ResearchResources
  unsettled_compute: number
  completed_rounds: number
  decisions_used: number
  deadline: number
  now: number
  bound_task_ids: readonly string[]
  user_cancelled: boolean
  approval_declined: boolean
  // Host-confirmed refused tasks with no grant/dispatch. These reservations
  // remain occupied and unaccounted, but cannot prevent recording the refusal.
  approval_declined_unsubmitted?: number
  approval_declined_refs?: readonly string[]
}

/** Deterministic evidence/contract checks after model JSON validation. This
 * grants no approval and never substitutes model text for a tool receipt. */
export function guardResearchAction(value: unknown, state: ResearchGuardState) {
  const contract = researchContract.parse(state.contract)
  const decision = parseResearchDecision(value, state.context)
  const observations = state.observations.map(verifyResearchObservation)
  const selected = observations.filter(item => decision.evidence_refs.includes(item.id))
  const reasons: string[] = []
  const exhausted = state.now >= state.deadline || state.decisions_used >= contract.limits.max_decisions
    || resourceKeys.some(key => {
      const limit = contract.limits.resources[key]
      return limit !== null && limit > 0 && state.resources[key] >= limit
    })
    || state.completed_rounds >= contract.limits.max_rounds
  const spending = decision.action === "prepare_action" || decision.action === "execute_prepared_action"
  if (spending && contract.mode === "offline-replay") reasons.push("Offline replay cannot start scientific computation")
  if (spending && exhausted) reasons.push("The cumulative research budget or round limit is exhausted")
  if (spending && state.unsettled_compute > 0) reasons.push("Previous compute is running or has unsettled usage")
  if (spending && state.user_cancelled) reasons.push("The user cancelled this research loop")

  if (decision.action === "finish") {
    const declinedOnly = decision.outcome === "approval_declined" && state.approval_declined
      && state.approval_declined_unsubmitted === state.unsettled_compute
    if (decision.outcome !== "user_cancelled" && state.unsettled_compute > 0 && !declinedOnly)
      reasons.push("Cannot finish while compute completion or accounting is unresolved")
    if (decision.outcome === "user_cancelled" && !state.user_cancelled)
      reasons.push("User cancellation needs a host-recorded user event")
    if (decision.outcome === "approval_declined" && !state.approval_declined)
      reasons.push("Approval refusal needs the native permission result")
    if (decision.outcome === "approval_declined" && !state.approval_declined_refs?.some(ref => decision.evidence_refs.includes(ref)))
      reasons.push("Approval refusal must cite its confirmed native permission evidence")
    if (decision.outcome === "budget_exhausted" && !exhausted)
      reasons.push("No exhausted cumulative limit has been established")
    if (decision.outcome === "no_viable_candidate"
      && !selected.some(item => item.classification === "zero_candidates"
        || item.classification === "scientific_failure" && item.counts.scientific_passed === 0))
      reasons.push("No-candidate conclusion requires an evaluated empty selection or failed scientific checks")
    if (decision.outcome === "objective_met") {
      if (contract.mode === "offline-replay") reasons.push("Offline replay cannot certify a new scientific objective")
      if (!selected.some(item => accepts(contract, item, state.bound_task_ids)))
        reasons.push("No cited observation meets the frozen scientific contract and run provenance")
    }
  }
  return { decision, accepted: reasons.length === 0, reasons,
    acceptance_scope: contract.mode,
    // Even a fully successful replay is never a new research result.
    new_research_result: contract.mode === "native-compute" && reasons.length === 0
      && decision.action === "finish" && decision.outcome === "objective_met",
    evaluated_observation_ids: selected.map(item => item.id) }
}

function accepts(contract: ResearchContract, observation: ResearchObservation, tasks: readonly string[]) {
  if (observation.classification !== "objective_met" || observation.identity.status !== "pass"
    || observation.counts.scientific_passed === null
    || observation.counts.scientific_passed < contract.quality.minimum_candidates
    || observation.counts.input_records !== contract.limits.max_library_records
    || observation.counts.selected === null
    || observation.counts.selected > contract.limits.max_candidates_per_round
    || observation.run.release_id !== contract.inputs.source_release
    || contract.quality.manifest_sha256 === null) return false
  if (contract.mode === "native-compute"
    && (observation.origin !== "native_execution" || !observation.run.task_id
      || !tasks.includes(observation.run.task_id))) return false
  const required = new Set(observation.required_scientific_checks)
  return contract.quality.required_checks.every(id => required.has(id)
    && observation.scientific_checks.some(check => check.id === id && check.status === "pass"))
    && observation.artifacts.some(item => item.sha256 === contract.quality.manifest_sha256
      && item.status === "verified")
}
