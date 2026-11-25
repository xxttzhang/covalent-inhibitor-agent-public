type Result = { version: string; task_id: string; plan_digest: string; rows: number; input_rows: number; eligible: number;
  incompatible: number; ambiguous: number; valid_warhead: number; limits: Record<string, number>;
  property_failures: Record<string, number>; valid_warhead_property_failures: Record<string, number>;
  files: Record<string, { sha256: string; bytes: number }>; scientific_quality_verified: false; compute_submitted: false }
type Diagnosis = { version: 1; task_id: string; plan_digest: string; recorded_at: number; state: "AVAILABLE" | "UNAVAILABLE";
  scientific_quality_verified: false; compute_submitted: false; note: string; reader?: string; result?: Result }
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value)
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 20000
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)
export const propertyReasons: Record<string, string> = { mw_below_min: "分子量低于下限", mw_above_max: "分子量高于上限",
  clogp_below_min: "cLogP 低于下限", clogp_above_max: "cLogP 高于上限", tpsa_above_max: "TPSA 高于上限",
  rotors_above_max: "可旋转键超过上限", charge_above_max: "电荷绝对值超过上限" }
const limits = ["min_mw", "max_mw", "min_logp", "max_logp", "max_tpsa", "max_rotors", "max_abs_formal_charge"]
const files = ["plan.json", "started.json", "error.json", "target.yaml", "selection.json", "master.parquet"]

export function preparationDiagnostic(value: unknown, task: string, digest: string): Diagnosis | undefined {
  if (!record(value) || value.version !== 1 || value.task_id !== task || value.plan_digest !== digest || !hash(digest) ||
    typeof value.recorded_at !== "number" || !Number.isSafeInteger(value.recorded_at) || value.recorded_at <= 0 ||
    value.scientific_quality_verified !== false || value.compute_submitted !== false || typeof value.note !== "string") return
  if (value.state === "UNAVAILABLE") return value as Diagnosis
  const result = value.result
  if (value.state !== "AVAILABLE" || !hash(value.reader) || !record(result) || result.version !== "preparation-diagnostic-v1" ||
    result.task_id !== task || result.plan_digest !== digest || result.scientific_quality_verified !== false || result.compute_submitted !== false ||
    !count(result.rows) || !count(result.input_rows) || !count(result.eligible) || !count(result.incompatible) ||
    !count(result.ambiguous) || !count(result.valid_warhead) || result.rows > result.input_rows || result.eligible > result.valid_warhead ||
    result.rows !== result.incompatible + result.ambiguous + result.valid_warhead || !record(result.limits) ||
    !record(result.files) || !record(result.property_failures) || !record(result.valid_warhead_property_failures)) return
  const thresholds = result.limits
  const inventory = result.files
  if (!limits.every((key) => typeof thresholds[key] === "number" && Number.isFinite(thresholds[key])) || !files.every((name) => {
    const file = inventory[name]
    return record(file) && hash(file.sha256) && typeof file.bytes === "number" && Number.isSafeInteger(file.bytes) && file.bytes > 0 && file.bytes <= 32 * 1024 * 1024
  })) return
  const all = result.property_failures
  const valid = result.valid_warhead_property_failures
  if (!Object.keys(propertyReasons).every((key) => count(all[key]) && all[key] <= Number(result.rows) &&
    count(valid[key]) && valid[key] <= Number(result.valid_warhead) && valid[key] <= all[key])) return
  return value as Diagnosis
}
