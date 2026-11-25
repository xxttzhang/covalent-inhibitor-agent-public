type Candidate = { input_sdf_index: number; compound_id: string; selection_channel: string;
  carsidock_rank_fraction: number; rtmscore_rank_fraction: number; rank_disagreement: number }
type Base = { version: 1; task_id: string; plan_digest: string; recorded_at: number;
  grants_resource_approval: false; additional_budget_points: null; proposal_submitted_compute: false }
type Available = Base & { state: "SUBSET_PROPOSED" | "NO_CANDIDATES_MEET_POLICY";
  selection_id: string; selection_manifest_digest: string; input_count: number; eligible_count: number;
  excluded_count: number; selected_count: number; effective_cap: number; calibrated_uncertainty: false; note: string;
  policy: { max_candidates: number; max_fraction: number; disagreement_threshold: number; top_fraction: number;
    uncertainty_count: number; consensus_count: number; diversity_count: number }; candidates: Candidate[] }
type Selection = Available | (Base & { state: "UNAVAILABLE"; reason: string })
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value)
const count = (value: unknown, max: number): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= max
const fraction = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)
export const selectionChannels: Record<string, string> = { rank_disagreement_review: "排名分歧复核",
  consensus_sentinel: "一致性对照", diversity_sentinel: "多样性对照" }

function valid(value: unknown): value is Selection {
  if (!record(value) || value.version !== 1 || typeof value.task_id !== "string" || !/^screen-[a-f0-9]{24}$/.test(value.task_id) ||
    !hash(value.plan_digest) || !count(value.recorded_at, Number.MAX_SAFE_INTEGER) || value.recorded_at === 0 ||
    value.grants_resource_approval !== false || value.additional_budget_points !== null || value.proposal_submitted_compute !== false) return false
  if (value.state === "UNAVAILABLE") return typeof value.reason === "string" && value.reason.length > 0
  if (!["SUBSET_PROPOSED", "NO_CANDIDATES_MEET_POLICY"].includes(String(value.state)) || !hash(value.selection_id) ||
    !hash(value.selection_manifest_digest) || value.calibrated_uncertainty !== false || typeof value.note !== "string" ||
    !count(value.input_count, 20000) || !count(value.eligible_count, 20000) || !count(value.excluded_count, 20000) ||
    !count(value.selected_count, 100) || !count(value.effective_cap, 100) || !record(value.policy)) return false
  const policy = value.policy
  if (!count(policy.max_candidates, 100) || policy.max_candidates === 0 || !fraction(policy.max_fraction) ||
    policy.max_fraction === 0 || policy.max_fraction > 0.25 || !fraction(policy.disagreement_threshold) ||
    policy.disagreement_threshold === 0 || !fraction(policy.top_fraction) || policy.top_fraction === 0 ||
    !count(policy.uncertainty_count, 100) || policy.uncertainty_count === 0 || !count(policy.consensus_count, 100) ||
    !count(policy.diversity_count, 100) || policy.uncertainty_count + policy.consensus_count + policy.diversity_count > policy.max_candidates) return false
  return value.input_count === value.eligible_count + value.excluded_count && value.selected_count <= value.effective_cap &&
    value.effective_cap === Math.min(policy.max_candidates, Math.floor(value.eligible_count * policy.max_fraction)) &&
    (value.state === "SUBSET_PROPOSED") === (value.selected_count > 0) && Array.isArray(value.candidates) &&
    value.candidates.length === value.selected_count && value.candidates.every((row) => record(row) &&
      count(row.input_sdf_index, Number.MAX_SAFE_INTEGER) && typeof row.compound_id === "string" && row.compound_id.length > 0 &&
      row.compound_id.length <= 256 && typeof row.selection_channel === "string" && Object.hasOwn(selectionChannels, row.selection_channel) &&
      fraction(row.carsidock_rank_fraction) && fraction(row.rtmscore_rank_fraction) && fraction(row.rank_disagreement))
}

// A detached metadata field must belong to this exact parent progress card.
export function inspireSelection(value: unknown, task: string, digest: string) {
  return valid(value) && value.task_id === task && value.plan_digest === digest ? value : undefined
}
