import policy from "../../../../../science/src/covscreen/docking-engine-policy.json"

export const dockingPolicy = policy
export const dockingPolicyMessage = "docking_engine_disabled: " + policy.reason
export const dockingRoutingSummary =
  "Docking engine policy: CarsiDock-Cov is enabled only for library-first; ligand-first and de-novo use CovDock without automatic fallback."

export function requireDockingEngine(engine: unknown, route?: string) {
  if (typeof engine !== "string") return
  const enabledByRoute = (policy as { enabled_engines_by_route?: Record<string, string[]> }).enabled_engines_by_route
  if (route && enabledByRoute?.[route]?.includes(engine)) return
  if (policy.disabled_engines.includes(engine)) throw Error(dockingPolicyMessage)
}

/** Check the backend's resolved plan, including retry requests, before approval. */
export function requireDockingScope(value: unknown, route?: string) {
  if (!value || typeof value !== "object") return
  if (Array.isArray(value)) return value.forEach(item => requireDockingScope(item, route))
  const record = value as Record<string, unknown>
  const currentRoute = typeof record.route === "string" ? record.route : route
  requireDockingEngine(record.engine, currentRoute)
  for (const item of Object.values(record)) requireDockingScope(item, currentRoute)
}

export function requireDockingTool(key: string, args: Record<string, unknown>) {
  const name = key.slice(key.lastIndexOf("covalent_"))
  if (name === "covalent_submit_job") {
    requireDockingEngine(args.engine, typeof args.route === "string" ? args.route : undefined)
  }
  if (name === "covalent_ligand_design_step" && ["plan_pose", "check_pose_runtime", "start_pose"].includes(String(args.action)))
    requireDockingEngine("carsidock-cov", "ligand-first")
}
