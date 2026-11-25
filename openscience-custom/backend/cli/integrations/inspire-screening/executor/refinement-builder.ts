import { createHash, randomBytes } from "node:crypto"
import z from "zod"
import { canonical, digest } from "./model"
import type { Store } from "./store"
import { refinementProfile, type RefinementProfile } from "./refinement-diagnostic"
import { refinementProposalSnapshot } from "./refinement-proposal"
import { refinementPaths } from "./refinement-runtime"
import { refinementPlan, Refinements } from "./refinement-approval"
import { refinementWorkflow, RefinementWorkflows } from "./refinement-workflow"
import { origin, requestHash, type BoundRequest } from "./request"
import path from "node:path"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { root } from "./host"
import { refinementSettings } from "./refinement-diagnostic"
import { Inspire } from "./inspire"
import { inspectWorkflowInterfaces, verifyWorkflowInterfaces } from "./engine-interface"
import { defaultRefinementDelivery } from "./refinement-delivery-policy"
import { planningEvent, retainPlanningEvent, verifyPlanningScope, refinementPlanningScope, type RefinementPlanningScope } from "./refinement-planning-scope"

const manifest = z.object({ version: z.literal("screening-code-release-v1"),
  files: z.record(z.string().min(1), z.string().regex(/^[a-f0-9]{64}$/)) }).strict()

// Convert the host's current witnessed proposal into exact plans, not an approval
// or a submission. Never accept model-supplied candidates/resources/argv. Source
// and runtime/license preflights remain mandatory before the native confirmation.
export function buildRefinementWorkflow(store: Store, taskID: string, expected: string,
  source: BoundRequest, input: RefinementProfile, release: unknown, now = Date.now(), scope?: RefinementPlanningScope) {
  const task = store.get(taskID)
  const snapshot = refinementProposalSnapshot(store, task, scope)
  if (snapshot?.state !== "AVAILABLE" || !snapshot.proposal) throw new Error("No current verified refinement proposal")
  const proposal = snapshot.proposal
  if (digest(proposal.planning_scope ?? null) !== digest(scope ?? null)) throw Error("Refinement compilation needs the exact research planning scope")
  if (scope && proposal.proposed_points > scope.max_points + 1e-8) throw Error("Refinement reservation exceeds the proposed independent budget")
  if (digest(proposal) !== expected) throw new Error("Refinement proposal changed before plan compilation")
  if (!Number.isSafeInteger(now) || now < proposal.created_at || now - proposal.resource_quote_recorded_at > 5 * 60000) {
    throw new Error("Refresh refinement planning before compiling stale resource quotes")
  }
  const profile = refinementProfile.parse(input)
  const environment = proposal.environment
  if (environment?.state !== "AVAILABLE" || !environment.result.assets_stable ||
    environment.result.profile_digest !== digest(profile) || digest(environment.result.code) !== digest(profile.code) ||
    task.plan.steps.some((stage) => stage.resource.project !== profile.project)) {
    throw new Error("Runtime profile differs from the proposal's witnessed environment")
  }
  const provenance = origin.parse(source.origin)
  if (provenance.request_sha256 !== requestHash(source.text)) throw new Error("Refinement request provenance changed")
  const inventory = manifest.parse(release)
  const encoded = JSON.stringify(canonical(inventory))
  if (digest(inventory) !== profile.code.release_id ||
    createHash("sha256").update(encoded + "\n").digest("hex") !== profile.code.manifest_sha256) {
    throw new Error("Source manifest differs from the diagnosed release")
  }
  if (Object.keys(inventory.files).length > 2000 || Object.keys(inventory.files).some((name) =>
    /[\\\x00-\x1f\x7f]/.test(name) || name.split("/").some((part) => !part || part === "." || part === ".."))) {
    throw new Error("Release manifest contains noncanonical or excessive paths")
  }
  if (inventory.files["code_release.py"] !== profile.code.checker_sha256) throw new Error("Release checker differs from diagnostic")
  const root = `${task.plan.runtime.root}/releases/${profile.code.release_id}`
  const assets = Object.fromEntries(Object.entries(inventory.files).map(([name, hash]) => [`${root}/${name}`, hash]))
  for (const file of environment.result.files) {
    if (file.state !== "HASHED" || !file.sha256) throw new Error("Runtime file lacks a witnessed checksum")
    if (assets[file.path] && assets[file.path] !== file.sha256) throw new Error("Runtime file conflicts with source manifest")
    assets[file.path] = file.sha256
  }
  for (const file of [
    { path: environment.result.af3.cpu_parameters.loader_path, sha256: environment.result.af3.cpu_parameters.loader_sha256 },
    { path: environment.result.covdock.entitlements.source_path, sha256: environment.result.covdock.entitlements.source_sha256 },
  ]) {
    if (file.path && file.sha256) assets[file.path] = file.sha256
  }
  const created = now
  const approve_before = created + 30 * 60000
  // Explicit scheduling allowance in addition to both bounded engine runtimes.
  // It is frozen in the approval, not an infinite queue/retry allowance.
  const finish_before = approve_before + (proposal.stages.reduce((sum, stage) => sum + stage.minutes, 0) + 60) * 60000
  const stages = proposal.stages.map((stage) => {
    const runtime = stage.engine === "af3" ? profile.af3 : profile.covdock
    if (runtime.name !== stage.engine) throw new Error("Proposal/runtime engine roles differ")
    const paths = refinementPaths(runtime, task.plan.runtime.root, profile.code.release_id)
    if (paths.files.some((file) => !assets[file])) throw new Error("Required executable/runtime role missing from witnessed inventory")
    // The actual model bytes, not only the runner, must stay fixed while queued.
    if (runtime.name === "af3" && !assets[`${runtime.model_dir}/af3.bin`]) throw new Error("AF3 weights lack a witnessed checksum")
    const engine = stage.engine === "af3" ? { name: "af3", ...stage.parameters,
      cpu_per_search: Math.min(2, Number(stage.resource.quota.split(",")[1])) }
      : { name: "covdock", ...stage.parameters }
    return refinementPlan.parse({ version: "screening-refinement-v1", id: "refine-" + randomBytes(12).toString("hex"),
      request: source.text, origin: provenance, directory: task.plan.directory, session: task.plan.session,
      parent_id: task.id, parent_digest: task.digest, report_digest: proposal.selection.source_manifest_digest,
      selection_id: proposal.selection.selection_id, selection_manifest_digest: proposal.selection.selection_manifest_digest,
      result_contract: "screening-refinement-outcome-v2", ...(stage.engine === "af3" ? { geometry_contract: "af3-geometry-evidence-v1" } : {}),
      quality_policy: proposal.quality_policy.policy, runtime_admission: "cpu-before-approval-gpu-at-start-v1",
      environment_contract: "no-loader-injection-v1",
      code: { release_id: profile.code.release_id, manifest_sha256: profile.code.manifest_sha256 },
      runtime, input: proposal.selection.input, engine, assets, resource: stage.resource, minutes: stage.minutes,
      max_points: stage.proposed_points, created, approve_before, finish_before })
  })
  const workflows = new RefinementWorkflows(new Refinements(store))
  const latest = workflows.planningSource(taskID)
  if (scope && latest) throw Error("Bound initial refinement cannot adopt an existing refinement; reconcile its original research operation")
  const previous = latest && workflows.failedBeforeCovdock(latest.id) ? workflows.get(latest.id) : null
  const recovery = !previous && latest ? workflows.recoverySource(latest.id) : null
  if ("continuation" in proposal) {
    const expected = previous ? { kind: "retry", id: previous.id, digest: previous.digest }
      : recovery ? { kind: "recovery", ...recovery.reference } : null
    if (digest(proposal.continuation) !== digest(expected)) throw Error("Proposal continuation changed before compilation")
  }
  const workflow = refinementWorkflow.parse({ version: "screening-refinement-workflow-v1",
    ...(previous ? { retry_of: { id: previous.id, digest: previous.digest } } : {}),
    ...(recovery ? { recovery_of: recovery.reference } : {}),
    id: "refineflow-" + randomBytes(12).toString("hex"), stages: recovery ? [recovery.previous.plan.stages[0], stages[1]] : stages, execution: "af3_then_covdock_same_subset",
    failure_policy: "stop_on_execution_failure", quality_policy: "retain_rejections_and_continue", max_points: recovery ? stages[1].max_points : proposal.proposed_points,
    delivery: defaultRefinementDelivery() })
  workflows.verifyRetry(workflow)
  // Snapshot verification above binds the current core/selection chain. Native
  // approval revalidates parent and runtime evidence; compilation inserts no rows.
  return { workflow, digest: digest(workflow), proposal_digest: expected,
    blockers: [...proposal.blockers, "FROZEN_RELEASE_REFINEMENT_INTERFACE_NOT_VERIFIED"], ready_for_approval: false as const, compute_submitted: false as const,
      note: recovery ? "保留原 AF3 结果，仅为 CovDock 生成新计划；尚未批准或启动计算。" : "已转换为固定双阶段计划；尚未持久化审批或启动计算。必须完成实时源文件、资源和合法运行条件复核，再统一确认。" }
}

// Local archive read only. The manifest must match the release already witnessed
// by the platform's remote diagnosis; local source presence is not deployment.
export async function prepareRefinementDraft(store: Store, taskID: string, source: BoundRequest, input?: RefinementPlanningScope) {
  const scope = input ? verifyPlanningScope(store, store.get(taskID), input) : undefined
  const previous = scope ? planningEvent(store, taskID, "refinement_workflow_draft", scope) : null
  if (previous) {
    const saved = JSON.parse(previous.payload)
    if (saved.workflow && (saved.workflow.stages[0].request !== source.text || digest(saved.workflow.stages[0].origin) !== digest(source.origin)))
      throw Error("Bound refinement draft belongs to another original request")
    return refinementDraftSnapshot(store, taskID, Date.now(), scope)
  }
  try {
    const task = store.get(taskID)
    const saved = refinementProposalSnapshot(store, task, scope)
    if (saved?.state !== "AVAILABLE") throw new Error("Proposal unavailable")
    const profile = await refinementSettings()
    const archive = Bun.file(path.join(root, ".cache/inspire-executor/code-artifacts", profile.code.release_id + ".tar.gz"))
    if (!await archive.exists() || archive.size > 64 * 1024 * 1024) throw new Error("Local release archive unavailable or oversized")
    const result = await promisify(execFile)("tar", ["-xOf", archive.name!, "manifest.json"], { timeout: 5000, maxBuffer: 1024 * 1024 })
    const built = buildRefinementWorkflow(store, taskID, digest(saved.proposal), source, profile, JSON.parse(result.stdout), Date.now(), scope)
    const interface_checks = await inspectWorkflowInterfaces(built.workflow, task, new Inspire(task.plan.connection))
    if (digest(await refinementSettings()) !== digest(profile)) throw new Error("Runtime profile changed")
    const current = refinementProposalSnapshot(store, store.get(taskID), scope)
    if (current?.state !== "AVAILABLE" || digest(current.proposal) !== built.proposal_digest) throw new Error("Proposal changed before draft retention")
    retainPlanningEvent(store, taskID, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...built, interface_checks }, scope)
    return refinementDraftSnapshot(store, taskID, Date.now(), scope)
  } catch {
    retainPlanningEvent(store, taskID, "refinement_workflow_draft", { state: "UNAVAILABLE", reason: "DRAFT_SOURCE_OR_PROPOSAL_VERIFICATION_FAILED" }, scope)
    if (scope) return refinementDraftSnapshot(store, taskID, Date.now(), scope)
    return { state: "UNAVAILABLE", reason: "DRAFT_SOURCE_OR_PROPOSAL_VERIFICATION_FAILED", ready_for_approval: false,
      compute_submitted: false, note: "执行草案生成失败，未保留旧草案替代，未创建审批或计算。" }
  }
}

export function refinementDraftSnapshot(store: Store, taskID: string, now = Date.now(), scope?: RefinementPlanningScope) {
  const row = planningEvent(store, taskID, "refinement_workflow_draft", scope)
  if (!row) return null
  const base = { recorded_at: row.at, ready_for_approval: false, compute_submitted: false }
  try {
    const saved = JSON.parse(row.payload)
    const bound = saved.planning_scope ? refinementPlanningScope.parse(saved.planning_scope) : undefined
    const workflow = refinementWorkflow.parse(saved.workflow)
    const current = refinementProposalSnapshot(store, store.get(taskID), bound)
    if (saved.state !== "COMPILED_NOT_EXECUTABLE" || saved.digest !== digest(workflow) ||
      current?.state !== "AVAILABLE" || saved.proposal_digest !== digest(current.proposal)) throw new Error("Draft no longer matches current proposal")
    const proposal = current.proposal
    if ("continuation" in proposal) {
      const expected = workflow.retry_of ? { kind: "retry", ...workflow.retry_of }
        : workflow.recovery_of ? { kind: "recovery", ...workflow.recovery_of } : null
      if (digest(proposal.continuation) !== digest(expected)) throw Error("Draft continuation differs from proposal")
    }
    if (workflow.recovery_of || workflow.retry_of) new RefinementWorkflows(new Refinements(store)).verifyRetry(workflow)
    const checks = saved.interface_checks ? verifyWorkflowInterfaces(saved.interface_checks, workflow, store.get(taskID)) : null
    const supported = checks?.stages.every((stage) => stage.state === "PASSED") === true
    workflow.stages.forEach((stage, index) => {
      const expected = proposal.stages[index]
      if (stage.parent_id !== taskID || stage.parent_digest !== proposal.plan_digest ||
        digest(stage.input) !== digest(proposal.selection.input) || digest(stage.quality_policy) !== digest(proposal.quality_policy.policy) ||
        stage.selection_id !== proposal.selection.selection_id || stage.selection_manifest_digest !== proposal.selection.selection_manifest_digest ||
        stage.engine.name !== expected.engine || (!(workflow.recovery_of && index === 0) && (digest(stage.resource) !== digest(expected.resource) ||
        stage.minutes !== expected.minutes || stage.max_points !== expected.proposed_points))) throw new Error("Draft scope differs from proposal")
    })
    const active = workflow.recovery_of ? workflow.stages[1] : workflow.stages[0]
    return { ...base, ...(bound ? { planning_scope: bound } : {}), state: now > active.approve_before ? "EXPIRED" : "COMPILED_NOT_EXECUTABLE",
      id: workflow.id, digest: saved.digest, proposal_digest: saved.proposal_digest,
      retry_of: workflow.retry_of ?? null,
      recovery_of: workflow.recovery_of ?? null,
      delivery_policy: workflow.delivery ?? null,
      input_sha256: workflow.stages[0].input.sha256, identity_sha256: workflow.stages[0].input.identity_sha256,
      code: active.code, stages: workflow.stages.map((stage, index) => ({ id: stage.id, engine: stage.engine.name,
        reused: !!workflow.recovery_of && index === 0,
        candidate_count: stage.input.candidates.length, minutes: workflow.recovery_of && index === 0 ? 0 : stage.minutes, max_points: workflow.recovery_of && index === 0 ? 0 : stage.max_points })),
      approve_before: active.approve_before, finish_before: active.finish_before,
      interface_checks: checks ? { checked_at: checks.checked_at, stages: checks.stages.map((stage) => ({ engine: stage.engine, state: stage.state })),
        scope: "saved_source_interface_and_quoted_rate", runtime_verified: false, license_checkout_verified: false } : null,
      blockers: [...proposal.blockers, ...(supported ? [] : ["FROZEN_RELEASE_REFINEMENT_INTERFACE_NOT_VERIFIED"])],
      note: "保存的不可执行草案，不是当前资源或许可证就绪证明；不自动恢复过期草案或申请审批。" }
  } catch {
    return { ...base, state: "UNAVAILABLE", note: "最新执行草案与方案不一致或不可用；未回退旧记录，未批准或启动计算。" }
  }
}
