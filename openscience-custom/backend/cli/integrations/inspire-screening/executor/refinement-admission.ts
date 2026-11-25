import { randomBytes } from "node:crypto"
import { digest } from "./model"
import type { Store } from "./store"
import { Refinements } from "./refinement-approval"
import { refinementWorkflow, RefinementWorkflows, executableRefinementStages, type RefinementWorkflow } from "./refinement-workflow"
import { refinementProfile, readRefinementDiagnostic, verifyRefinementDiagnostic } from "./refinement-diagnostic"
import { ReviewRequired } from "./runner"
import { Inspire } from "./inspire"
import { RefinementRuns } from "./refinement-runs"
import { RefinementPreparation } from "./refinement-preparation"
import { RefinementReadbacks } from "./refinement-readback"
import { evaluateRefinementQuality } from "./refinement-quality"

// A fixed production prerequisite, not an arbitrary callback or model verdict.
// GPU admission occurs inside the separately approved allocation and must pass
// before the actual AF3 wrapper is launched. CPU inspection grants no resources.
export async function admitRefinement(store: Store, input: RefinementWorkflow, signal: AbortSignal,
  read = readRefinementDiagnostic) {
  const workflow = refinementWorkflow.parse(input)
  const approvals = new Refinements(store)
  const [af3, covdock] = workflow.stages
  if (workflow.recovery_of) new RefinementWorkflows(approvals).verifyRetry(workflow)
  if (workflow.stages.some((stage) => stage.runtime_admission !== "cpu-before-approval-gpu-at-start-v1" || !stage.quality_policy) || !workflow.delivery) {
    throw new ReviewRequired("Production refinement requires explicit startup, structural QC and delivery contracts")
  }
  const parent = approvals.parent(af3)
  approvals.parent(covdock)
  const identity = digest({ parent, stages: store.stages(parent.id) })
  const bound = () => {
    signal.throwIfAborted()
    if (identity !== digest({ parent: store.get(parent.id), stages: store.stages(parent.id) })) throw new ReviewRequired("Core changed during runtime admission")
  }
  bound()
  if (workflow.recovery_of) {
    const runs = new RefinementRuns(approvals)
    const owner = randomBytes(16).toString("hex")
    if (!runs.acquire(af3.id, owner)) throw new ReviewRequired("Original AF3 result is currently being inspected")
    const heartbeat = setInterval(() => runs.renew(af3.id, owner), 10000)
    try {
      const files = await new RefinementReadbacks(new RefinementPreparation(runs, new Inspire(parent.plan.connection)))
        .read(af3, runs.owned(af3.id, owner))
      runs.owned(af3.id, owner)
      const result = evaluateRefinementQuality(af3, parent, files)
      if (digest(result) !== workflow.recovery_of.af3_result_digest) throw new ReviewRequired("Fresh AF3 files differ from reused result")
      new RefinementWorkflows(approvals).verifyRetry(workflow)
      bound()
    } finally {
      clearInterval(heartbeat)
      runs.release(af3.id, owner)
    }
  }
  const active = executableRefinementStages(workflow)[0]
  const profile = refinementProfile.parse({ version: "refinement-environment-profile-v1", project: active.resource.project,
    target: parent.plan.target, site: parent.plan.site, code: { ...active.code,
      checker_sha256: active.assets[`${parent.plan.runtime.root}/releases/${active.code.release_id}/code_release.py`] },
    af3: af3.runtime, covdock: covdock.runtime })
  const nonce = randomBytes(16).toString("hex")
  const result = verifyRefinementDiagnostic(await read(parent, profile, nonce), parent, profile, nonce)
  bound()
  const sources = [...result.files.map((file) => ({ path: file.path, sha256: file.sha256 })),
    { path: result.af3.cpu_parameters.loader_path, sha256: result.af3.cpu_parameters.loader_sha256 },
    { path: result.covdock.entitlements.source_path, sha256: result.covdock.entitlements.source_sha256 }]
  const changed = sources.some((source) => !source.path || !source.sha256 ||
    executableRefinementStages(workflow).some((stage) => stage.assets[source.path!] !== source.sha256))
  const blockers = result.blockers.filter((code) => code !== "AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED")
  if (result.af3.cpu_parameters.state !== "PASSED") blockers.push("AF3_CPU_PARAMETERS_NOT_VERIFIED")
  if (result.covdock.entitlements.state !== "PASSED" || result.covdock.license.returncode !== 0) blockers.push("VENDOR_LICENSE_PREREQUISITES_FAILED")
  if (changed) blockers.push("FROZEN_RUNTIME_ASSETS_CHANGED")
  const evidence = { workflow_id: workflow.id, workflow_digest: digest(workflow), checked_at: Date.now(),
    state: blockers.length ? "BLOCKED" : "PASSED", blockers: [...new Set(blockers)],
    diagnostic: result, gpu_verified: false, compute_submitted: false,
    gpu_admission: "inside-approved-job-before-inference" }
  store.event(parent.id, "refinement_runtime_admission", evidence)
  if (blockers.length) throw new ReviewRequired("Refinement runtime admission: " + evidence.blockers.join(", "))
  return evidence
}
