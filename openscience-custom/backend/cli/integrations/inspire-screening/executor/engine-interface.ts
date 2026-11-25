import z from "zod"
import { quote, type Inspire } from "./inspire"
import bootstrap from "./interface_bootstrap.py" with { type: "text" }
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { digest, type Task } from "./model"
import { executableRefinementStages, type RefinementWorkflow } from "./refinement-workflow"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const result = z.object({ engine: z.enum(["af3", "covdock"]), entry: z.string(), entry_sha256: hash,
  helper: z.string(), helper_sha256: hash.nullable(), missing_options: z.array(z.string()),
  exact_input_guard_present: z.boolean(), passes_static_checks: z.boolean(), blocking_reasons: z.array(z.string()),
  runtime_verified: z.literal(false), license_checkout_verified: z.literal(false), resource_approval_granted: z.literal(false),
  code: z.object({ release_id: hash, manifest_sha256: hash }).strict(),
  input_score_sources: z.array(z.string()).optional(),
  geometry_contracts: z.array(z.string()).optional(),
  original_structure_readback_supported: z.boolean().optional(),
  quality_policies: z.array(z.string()).optional(),
}).passthrough()

export function verifyEngineInterface(value: unknown, input: RefinementPlan, task: Task) {
  const plan = refinementPlan.parse(input)
  if (plan.parent_id !== task.id || plan.parent_digest !== task.digest || plan.directory !== task.plan.directory || plan.session !== task.plan.session) {
    throw new Error("Refinement interface source identity differs")
  }
  const data = result.parse(value)
  if (data.code.release_id !== plan.code.release_id || data.code.manifest_sha256 !== plan.code.manifest_sha256) throw new Error("Refinement code release differs from approval")
  const root = `${task.plan.runtime.root}/releases/${plan.code.release_id}/science`
  const entry = `${root}/scripts/${plan.engine.name === "af3" ? "run_af3_registered_batch.py" : "run_covdock_lo_registered.py"}`
  const helper = `${root}/src/covscreen/refinement_input.py`
  if (data.engine !== plan.engine.name || data.entry !== entry || data.helper !== helper) throw new Error("Unexpected refinement entry or guard module")
  if (plan.engine.name === "covdock" && !data.input_score_sources?.includes("core")) throw new Error("CovDock entry lacks explicit core score provenance support")
  if (plan.engine.name === "covdock" && plan.result_contract === "screening-refinement-outcome-v2" &&
    data.original_structure_readback_supported !== true) throw new Error("CovDock release lacks original structure SDK readback support")
  if (plan.geometry_contract && !data.geometry_contracts?.includes(plan.geometry_contract)) throw new Error("Frozen release lacks approved AF3 geometry readback support")
  if (plan.quality_policy && !data.quality_policies?.includes(plan.quality_policy.version)) throw new Error("Frozen release lacks approved structural QC measurement support")
  if (!data.passes_static_checks || !data.exact_input_guard_present || data.missing_options.length || data.blocking_reasons.length || !data.helper_sha256) {
    throw new Error("Refinement entry lacks exact-input support: " + data.blocking_reasons.join("; "))
  }
  if (plan.assets[entry] !== data.entry_sha256 || plan.assets[helper] !== data.helper_sha256) {
    throw new Error("Refinement entry/guard source is absent from or differs from frozen assets")
  }
  return data
}

// A necessary source-and-price gate, not overall engine/license readiness.
export async function preflightRefinementInterface(plan: RefinementPlan, task: Task,
  transport: Pick<Inspire, "bound" | "call" | "live">) {
  transport.bound(task)
  const reply = z.object({ returncode: z.literal(0), output: z.string().max(64 * 1024) }).parse(
    await transport.call(refinementInterfaceArgs(plan, task)))
  const data = verifyEngineInterface(JSON.parse(reply.output), plan, task)
  const price = await transport.live(plan.resource)
  if (price !== plan.resource.points_per_hour) throw new Error("Refinement quota price changed; independent resource approval must be regenerated")
  return data
}

export function refinementInterfaceArgs(input: RefinementPlan, task: Task) {
  const plan = refinementPlan.parse(input)
  if (plan.parent_id !== task.id || plan.parent_digest !== task.digest || plan.directory !== task.plan.directory || plan.session !== task.plan.session) {
    throw new Error("Interface request belongs to another core task")
  }
  const release = `${task.plan.runtime.root}/releases/${plan.code.release_id}`
  const packet = { root: task.plan.runtime.root, engine: plan.engine.name, ...plan.code,
    checker_sha256: hash.parse(plan.assets[`${release}/code_release.py`]) }
  const command = ["env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
    "python3", "-I", "-B", "-c", bootstrap, Buffer.from(JSON.stringify(packet)).toString("base64")].map(quote).join(" ")
  return ["notebook", "exec", task.plan.connection.notebook, "--workspace", task.plan.connection.workspace, "--timeout", "60", command]
}

const checks = z.object({ workflow_digest: hash, checked_at: z.number().int().nonnegative(),
  stages: z.array(z.discriminatedUnion("state", [
    z.object({ engine: z.enum(["af3", "covdock"]), plan_digest: hash, state: z.literal("PASSED"), evidence: result }).strict(),
    z.object({ engine: z.enum(["af3", "covdock"]), plan_digest: hash, state: z.literal("UNAVAILABLE") }).strict(),
  ])).min(1).max(2) }).strict()

export function verifyWorkflowInterfaces(value: unknown, workflow: RefinementWorkflow, task: Task) {
  const saved = checks.parse(value)
  if (saved.workflow_digest !== digest(workflow)) throw new Error("Interface checks belong to another workflow")
  const executable = executableRefinementStages(workflow)
  if (saved.stages.length !== executable.length) throw new Error("Interface check execution coverage changed")
  saved.stages.forEach((stage, index) => {
    const plan = executable[index]
    if (stage.engine !== plan.engine.name || stage.plan_digest !== digest(plan)) throw new Error("Interface check stage identity changed")
    if (stage.state === "PASSED") verifyEngineInterface(stage.evidence, plan, task)
  })
  return saved
}

export async function inspectWorkflowInterfaces(workflow: RefinementWorkflow, task: Task,
  transport: Pick<Inspire, "bound" | "call" | "live">) {
  const stages = []
  // Read each exact released interface; no calls to create/submit/stage engines.
  for (const plan of executableRefinementStages(workflow)) {
    const binding = { engine: plan.engine.name, plan_digest: digest(plan) }
    try {
      const evidence = await preflightRefinementInterface(plan, task, transport)
      stages.push({ ...binding, state: "PASSED", evidence })
    } catch {
      stages.push({ ...binding, state: "UNAVAILABLE" })
    }
  }
  return verifyWorkflowInterfaces({ workflow_digest: digest(workflow), checked_at: Date.now(), stages }, workflow, task)
}
