import { z } from "zod"

/** Versioned interpretation of producer measurements. No thresholds or decisions.
 * IDs alone never establish equivalence: stage, unit and definition must match.
 * Historical or unknown definitions remain readable without reinterpretation.
 */
export const metricSemanticsVersion = "metric-semantics-v1"
/** Empty CSV cells and missing values must not become Number("") === 0. */
export function reportedNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null
  if (typeof value === "string" && !value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
export const metricInterpretationInstructions = `Preserve producer values, units, definitions, stages, and provenance when reporting metrics; use handoff interpretations only when state=registered (unregistered/definition_mismatch means unestablished, not failed). Treat missing/null values as unknown, never zero or pass. Do not reuse prereaction limits for adducts, compare scores across uncalibrated methods, or equate structural QC/geometry with kinetics, selectivity, or biological activity.`
export const metricInterpretation = z.union([
  z.object({ state: z.enum(["unregistered", "definition_mismatch"]),
    registry: z.literal(metricSemanticsVersion), reason: z.string() }).strict(),
  z.object({ state: z.literal("registered"), registry: z.literal(metricSemanticsVersion),
    supports: z.string(), applicability: z.string(), doesNotEstablish: z.string(),
    thresholdPolicy: z.string() }).strict(),
])
export const prereactionGeometry = [
  { id: "SG_beta_distance_A", label: "SG–beta distance", unit: "Å",
    definition: "Prereaction distance between receptor SG and ligand beta carbon",
    supports: "Spatial proximity in the reported prereaction pose" },
  { id: "SG_beta_alpha_angle_deg", label: "SG–beta–alpha angle", unit: "deg",
    definition: "Prereaction angle SG–beta–alpha, vertex at ligand beta carbon",
    supports: "Orientation of SG relative to the mapped beta–alpha bond in the reported pose" },
] as const

const mdMeasurements = [
  { key: "whole_ligand_RMSD_A", ligandID: "whole_ligand_rmsd", label: "配体重原子 RMSD", unit: "Å", scale: 1,
    supports: "Displacement of ligand heavy atoms relative to the analysis reference and alignment" },
  { key: "largest_ring_system_RMSD_A", ligandID: "largest_ring_rmsd", label: "最大环系 RMSD", unit: "Å", scale: 1,
    supports: "Displacement of the selected ring system under the recorded atom selection and alignment" },
  { key: "ligand_heavy_COM_displacement_A", ligandID: "ligand_com_displacement", label: "配体重原子 COM 位移", unit: "Å", scale: 1,
    supports: "Displacement of ligand heavy-atom center of mass under the recorded coordinate processing" },
  { key: "initial_contact_retention_excluding_tether", ligandID: "initial_contact_retention", label: "初始非共价接触保持", unit: "%", scale: 100,
    supports: "Retention of the analysis-defined initial contact set excluding the covalent tether" },
] as const

/** Preserve historical IDs and definitions while sharing measurement ownership.
 * Counts refer to independently initialized trajectories, not trajectory frames.
 */
export function mdMetricDefinitions(route: "library-first" | "ligand-first", replicas: number) {
  if (!Number.isInteger(replicas) || replicas < 1 || replicas > 3 || (route === "library-first" && replicas !== 3))
    throw Error("MD metric profile requires its audited replica coverage")
  return mdMeasurements.filter(entry => route === "ligand-first" || entry.key !== "largest_ring_system_RMSD_A")
    .map(entry => ({ ...entry, id: route === "library-first" ? entry.key : entry.ligandID,
      definition: route === "library-first" ? "全部 0–10 ns 生产段；三条独立轨迹时间均值的平均 ± 重复间 SD"
        : `各已审计 0–10 ns 轨迹的时间均值；候选值为 ${replicas} 个独立重复的均值${entry.key === "initial_contact_retention_excluding_tether" ? "（不含共价锚定接触）" : ""}。`,
    }))
}

export function interpretMetric(value: { id: string; unit?: string; definition?: string }, stage?: string) {
  const md = [...mdMetricDefinitions("library-first", 3),
    ...[1, 2, 3].flatMap(count => mdMetricDefinitions("ligand-first", count))]
    .filter(entry => entry.id === value.id)
  if (md.length) {
    const entry = md.find(entry => stage === "md" && value.unit === entry.unit && value.definition === entry.definition)
    if (!entry) return { state: "definition_mismatch" as const, registry: metricSemanticsVersion,
      reason: "MD stage, unit or audited aggregation definition differs; preserve the original value without reinterpretation" }
    return { state: "registered" as const, registry: metricSemanticsVersion, supports: entry.supports,
      applicability: "Audited 0–10 ns trajectory means with recorded alignment, atom/contact selection and independent initialization; SD is between replica means",
      doesNotEstablish: "Sampling convergence, binding free energy, reaction kinetics, selectivity or biological activity; a covalent tether constrains ligand motion",
      thresholdPolicy: "Descriptive trajectory observation, no universal stability pass/fail; single-replica uncertainty remains unavailable" }
  }
  const entry = prereactionGeometry.find(item => item.id === value.id)
  if (!entry) return { state: "unregistered" as const, registry: metricSemanticsVersion,
    reason: "No reviewed interpretation registered; preserve the producer definition" }
  if (stage !== "prereaction" || value.unit !== entry.unit || value.definition !== entry.definition)
    return { state: "definition_mismatch" as const, registry: metricSemanticsVersion,
      reason: "Stage, unit or definition differs from the registered metric; do not apply its interpretation" }
  return { state: "registered" as const, registry: metricSemanticsVersion,
    supports: entry.supports,
    applicability: "Prereaction coordinates with explicit receptor SG and ligand beta/alpha atom mapping; no universal target or warhead threshold",
    doesNotEstablish: "Covalent bond formation, reaction rate, binding affinity, selectivity or biological activity",
    thresholdPolicy: "Requires a separately versioned target/warhead/stage policy; this registry assigns no pass/fail",
  }
}
