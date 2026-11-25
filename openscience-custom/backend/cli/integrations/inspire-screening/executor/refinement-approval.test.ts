import { expect, test } from "bun:test"
import path from "node:path"

test("delivery policy imports without science assets but plan compilation still requires them", () => {
  const module = path.join(import.meta.dir, "refinement-delivery-policy.ts")
  const child = Bun.spawnSync([process.execPath, "-e",
    `const { defaultRefinementDelivery } = await import(${JSON.stringify(module)}); let rejected = false; try { defaultRefinementDelivery() } catch { rejected = true } if (!rejected) throw Error("missing assets accepted"); console.log("imported; compilation rejected")`],
    { env: { ...process.env, COVALENT_PIPELINE_ROOT: "/nonexistent-release-test-science" } })
  expect(child.exitCode).toBe(0)
  expect(child.stdout.toString().trim()).toBe("imported; compilation rejected")
})
import { Store } from "./store"
import { canonical, digest, planSchema } from "./model"
import { Refinements, refinementPlan, proposeRefinement } from "./refinement-approval"
import { requestHash } from "./request"
import { Instance } from "../../../src/project/instance"
import { PermissionNext } from "../../../src/permission/next"
import { Bus } from "../../../src/bus"
import { tmpdir } from "../../../test/fixture/fixture"
import { verifySelectionEvidence, verifyRefinementSelection, proposeVerifiedRefinement, proposeVerifiedRefinementWorkflow } from "./selection-evidence"
import { Inspire } from "./inspire"
import { verifyEngineInterface, refinementInterfaceArgs, verifyWorkflowInterfaces, inspectWorkflowInterfaces } from "./engine-interface"
import { refinementPaths } from "./refinement-runtime"
import { refinementCommand } from "./refinement-command"
import { RefinementRuns } from "./refinement-runs"
import { RefinementRunner, type RefinementTransport } from "./refinement-runner"
import { RefinementInspire, refinementTransport } from "./refinement-inspire"
import { RefinementPreparation, preparationArgs, verifyPreparation } from "./refinement-preparation"
import { RefinementReadbacks, verifyReadback, verifyReadbackOutcome } from "./refinement-readback"
import { verifyRefinementResult } from "./refinement-result"
import { ReviewRequired } from "./runner"
import type { RefinementPlan } from "./refinement-approval"
import { chmod, mkdir, open } from "node:fs/promises"
import { selectRefinementProposal, readSelectionProposal, verifySelectionProposal } from "./selection-proposal"
import { RefinementWorkflows, refinementWorkflow, proposeRefinementWorkflow } from "./refinement-workflow"
import { diagnoseRefinement, refinementDiagnosticArgs, refinementDiagnosticSnapshot, refinementProfile, verifyRefinementDiagnostic } from "./refinement-diagnostic"
import { refinementProposal, refinementProposalSnapshot, refinementProposalVersion, refinementSelectionPolicy, planRefinement, boundRefinementProposalVersion } from "./refinement-proposal"
import { schema as screeningToolSchema } from "./tool"
import { defaultRefinementQuality, provisionalRefinementQuality } from "./refinement-quality-policy"
import { evaluateRefinementQuality } from "./refinement-quality"
import { presentRefinement } from "./refinement-presentation"
import { buildRefinementWorkflow, refinementDraftSnapshot, prepareRefinementDraft } from "./refinement-builder"
import { combineRefinementReport, readRefinementReport } from "./refinement-report"
import { RefinementDelivery, refinementDeliveries, refinementInventory, approvedRefinementDeliveries } from "./refinement-delivery"
import { defaultRefinementDelivery } from "./refinement-delivery-policy"
import { refinementReportFiles } from "./refinement-report-files"
import { Publications } from "./publication"
import { Session } from "../../../src/session"
import { Server } from "../../../src/server/server"
import { ArtifactStore } from "../../../src/artifact/store"
import { admitRefinement } from "./refinement-admission"
import { runRefinements, refinementExecutions, approveRefinementExecution } from "./refinement-execution"
import { prepareWorkflowExecution } from "./workflow-execution"
import { refinementPlanningScope, planningEvent, retainPlanningEvent, closeRefinementPlanning, readRefinementPlanningClosure } from "./refinement-planning-scope"
import { refinementPlanningObservation } from "./workflow-refinement"
import { readRefinementObservation, advanceNativeRefinement } from "./refinement-lifecycle"
import { projectRefinementReport, readWorkflowRefinementResults, resumeWorkflowRefinementDelivery, mdCandidateChoice } from "./workflow-refinement-results"
import { readMdNativeTarget } from "./workflow-md-transfer"
import { fixture as researchFixture } from "./workflow-plan.fixture"
import { prepareWorkflowScreening } from "./workflow-plan"
import { workflowObservation } from "./workflow-execution"
import { workflowLedgerIdentity } from "./workflow-host"
import { exerciseMdHost } from "./workflow-md-host.fixture"
import { failurePacket, readRefinementFailure, refinementFailureSummary } from "./refinement-failure"

test("workflow approval reserves WAL writer before reading provenance", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const concurrent = new Store(path.join(tmp.path, "refinement.sqlite"))
  concurrent.db.exec("PRAGMA busy_timeout=0")
  try {
    const plan = workflowFixture(data)
    new RefinementWorkflows(data.refinements).create(plan)
    let attempted = false
    // A real second SQLite connection tries to commit after the first read,
    // exactly where a deferred WAL transaction would lose its write snapshot.
    class Interleaved extends RefinementWorkflows {
      override get(id: string) {
        const value = super.get(id)
        if (!attempted) {
          attempted = true
          expect(() => concurrent.event(data.task.id, "concurrent_probe", {})).toThrow("database is locked")
        }
        return value
      }
    }
    const workflows = new Interleaved(data.refinements)
    const approved = workflows.approve(plan.id, digest(plan), {
      directory: tmp.path, sessionID: plan.stages[0].session,
      abort: new AbortController().signal, metadata: () => {}, ask: async () => {},
    })
    expect(attempted).toBe(true)
    expect(approved.state).toBe("APPROVED")
    concurrent.event(data.task.id, "writer_released", {})
    expect(concurrent.db.query("SELECT COUNT(*) AS count FROM events WHERE task_id=? AND kind='writer_released'").get(data.task.id)).toEqual({ count: 1 })
    expect(data.store.db.query("SELECT COUNT(*) AS count FROM events WHERE task_id=? AND kind='refinement_workflow_approved'").get(data.task.id)).toEqual({ count: 1 })
  } finally { concurrent.close(); data.store.close() }
})

for (const mode of ["recover", "execute", "cancel", "running", "changed_result", "changed_scope", "changed_af3", "duplicate"] as const) {
  test(`CovDock-only recovery preserves completed AF3 and requires a fresh grant: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path)
    try {
      const workflows = new RefinementWorkflows(data.refinements)
      const prior = workflowFixture(data)
      const context = { directory: tmp.path, sessionID: prior.stages[0].session,
        abort: new AbortController().signal, metadata: () => {}, ask: async () => {} }
      workflows.create(prior)
      workflows.approve(prior.id, digest(prior), context)
      const runs = new RefinementRuns(data.refinements)
      const result = classifiedFixture(prior.stages[0], 0)
      for (const stage of prior.stages) {
        runs.ensure(stage.id); runs.acquire(stage.id, "fixture")
        const reserved = runs.reserve(stage.id, "fixture")
        if (stage.engine.name === "af3") {
          runs.update(stage.id, "fixture", "VERIFYING", null, reserved.name)
          runs.complete(stage.id, "fixture", result)
        } else runs.update(stage.id, "fixture", mode === "running" ? "RUNNING" : "FAILED", "Local contract fixture", reserved.name)
        runs.release(stage.id, "fixture")
      }
      const af3before = digest({ plan: data.refinements.get(prior.stages[0].id), run: runs.get(prior.stages[0].id) })
      const oldbefore = digest(workflows.get(prior.id))
      const covdock = refinementPlan.parse({ ...prior.stages[1], id: "refine-" + "9".repeat(24),
        environment_contract: "no-loader-injection-v1",
        created: prior.stages[1].created + 1,
        ...(mode === "changed_scope" ? { engine: { ...prior.stages[1].engine, min_coverage: 0.99 } } : {}) })
      const plan = refinementWorkflow.parse({ ...prior, id: "refineflow-" + "e".repeat(24),
        recovery_of: { id: prior.id, digest: digest(prior), af3_result_digest: mode === "changed_result" ? "0".repeat(64) : digest(result) },
        stages: [mode === "changed_af3" ? { ...prior.stages[0], id: "refine-" + "f".repeat(24) } : prior.stages[0], covdock],
        max_points: covdock.max_points })
      if (["running", "changed_result", "changed_scope", "changed_af3"].includes(mode)) {
        expect(() => workflows.create(plan)).toThrow()
      } else {
        workflows.create(plan)
        expect(workflows.planningSource(data.task.id)?.id).toBe(plan.id)
        expect(() => runs.ensure(covdock.id)).toThrow()
        if (mode === "cancel") {
          workflows.endApproval(plan.id, digest(plan), context)
          expect(data.refinements.get(covdock.id).state).toBe("CANCELLED")
          expect(workflows.planningSource(data.task.id)?.id).toBe(prior.id)
          expect(workflows.recoverySource(workflows.planningSource(data.task.id)!.id)?.reference.af3_result_digest).toBe(digest(result))
        } else {
          workflows.approve(plan.id, digest(plan), context)
          expect(workflows.authorize(covdock.id).id).toBe(plan.id)
          expect(workflows.authorize(prior.stages[0].id).id).toBe(prior.id)
          runs.ensure(covdock.id)
          expect(runs.ready(covdock.id)).toBe(true)
          if (mode === "execute") {
            // Contract transport only: no molecular calculation or remote claim.
            const calls: string[] = []
            const record = (stage: RefinementPlan, action: string) => {
              expect(stage.id).toBe(covdock.id)
              calls.push(action)
            }
            const runner = new RefinementRunner(runs, {
              prepare: async (stage) => { record(stage, "prepare") },
              preflight: async (stage) => { record(stage, "preflight") },
              submit: async (stage, run) => { record(stage, "submit"); return { handle: run.name } },
              observe: async (stage, run) => { record(stage, "observe"); return { state: "succeeded", handle: run.name } },
              stop: async () => { throw new Error("Unexpected stop") },
              verify: async (stage) => { record(stage, "verify"); return classifiedFixture(stage, 0) },
            })
            await runner.tickWorkflow(plan.id)
            await runner.tickWorkflow(plan.id)
            await runner.tickWorkflow(plan.id)
            expect(calls).toEqual(["prepare", "preflight", "submit", "observe", "verify"])
            expect(runs.get(covdock.id).state).toBe("SUCCEEDED")
          }
          if (mode === "duplicate") expect(() => workflows.create({ ...plan, id: "refineflow-" + "a".repeat(24) })).toThrow("already has")
        }
      }
      expect(digest({ plan: data.refinements.get(prior.stages[0].id), run: runs.get(prior.stages[0].id) })).toBe(af3before)
      expect(digest(workflows.get(prior.id))).toBe(oldbefore)
    } finally { data.store.close() }
  })
}

for (const mode of ["failed", "running", "scope", "identity", "duplicate"] as const) {
  test(`repair retry retains original failure and requires a new exact approval: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path)
    try {
      const workflows = new RefinementWorkflows(data.refinements)
      const previous = workflowFixture(data)
      workflows.create(previous)
      const context = { directory: tmp.path, sessionID: previous.stages[0].session,
        abort: new AbortController().signal, metadata: () => {}, ask: async () => {} }
      workflows.approve(previous.id, digest(previous), context)
      const runs = new RefinementRuns(data.refinements)
      for (const stage of previous.stages) { runs.ensure(stage.id); runs.acquire(stage.id, "fixture") }
      const reserved = runs.reserve(previous.stages[0].id, "fixture")
      runs.update(previous.stages[0].id, "fixture", mode === "running" ? "RUNNING" : "FAILED", "Local fixture failure", reserved.name)
      runs.cancel(previous.stages[1].id, tmp.path)
      runs.update(previous.stages[1].id, "fixture", "CANCELLED")
      for (const stage of previous.stages) runs.release(stage.id, "fixture")
      const before = digest({ previous: workflows.get(previous.id), runs: runs.list(), core: data.store.stages(data.task.id) })
      const retry = refinementWorkflow.parse({ ...previous, id: "refineflow-" + "f".repeat(24),
        retry_of: { id: previous.id, digest: mode === "identity" ? "0".repeat(64) : digest(previous) },
        stages: previous.stages.map((stage, index) => ({ ...stage, id: "refine-" + String(index + 7).repeat(24),
          created: stage.created + 1, ...(mode === "scope" ? { minutes: stage.minutes - 1 } : {}) })) })
      if (["running", "scope", "identity"].includes(mode)) {
        expect(() => workflows.create(retry)).toThrow()
      } else {
        const draft = workflows.create(retry)
        expect(draft.state).toBe("DRAFT")
        expect(draft.approval).toBeNull()
        expect(() => runs.ensure(retry.stages[0].id)).toThrow()
        if (mode === "duplicate") expect(() => workflows.create({ ...retry, id: "refineflow-" + "e".repeat(24) })).toThrow("already has")
      }
      expect(digest({ previous: workflows.get(previous.id), runs: runs.list(), core: data.store.stages(data.task.id) })).toBe(before)
    } finally { data.store.close() }
  })
}

for (const mode of ["complete", "changed", "lost_read", "running", "wrong_session", "review", "review_running", "review_unknown", "review_changed", "review_identity", "review_connection"] as const) {
  test(`failed refinement reads actual isolated files without granting or restarting compute: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path)
    try {
      const parent = data.store.get(data.task.id)
      const workflow = workflowFixture(data)
      const workflows = new RefinementWorkflows(data.refinements)
      workflows.create(workflow)
      workflows.approve(workflow.id, digest(workflow), { directory: tmp.path, sessionID: parent.plan.session,
        abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
      const runs = new RefinementRuns(data.refinements)
      const af3 = workflow.stages[0]
      runs.ensure(af3.id); runs.acquire(af3.id, "fixture")
      const reserved = runs.reserve(af3.id, "fixture")
      runs.update(af3.id, "fixture", mode.startsWith("review") ? "NEEDS_REVIEW" : mode === "running" ? "RUNNING" : "FAILED", "Explicit local fixture failure", reserved.name)
      runs.release(af3.id, "fixture")
      const covdock = workflow.stages[1]
      runs.ensure(covdock.id); runs.acquire(covdock.id, "fixture")
      runs.cancel(covdock.id, tmp.path); runs.update(covdock.id, "fixture", "CANCELLED"); runs.release(covdock.id, "fixture")
      const directory = `${tmp.path}/tasks/${parent.id}/refinements/${af3.id}`
      await mkdir(`${directory}/af3`, { recursive: true })
      await Bun.write(`${directory}/plan.json`, JSON.stringify(canonical({ plan: af3, digest: digest(af3) })) + "\n")
      await Bun.write(`${directory}/af3/engine.log`, "NON-SCIENTIFIC LOCAL FAILURE FIXTURE\n")
      const before = digest({ core: data.store.stages(parent.id), tasks: data.store.list(), runs: runs.list(), workflow: workflows.get(workflow.id) })
      const calls = { count: 0, observations: 0 }
      const cli = { bound: () => parent, call: async (args: string[]) => {
        if (args[1] === "status") {
          expect(args).toEqual([af3.resource.kind, "status", reserved.name, "--workspace", af3.resource.workspace])
          calls.observations++
          if (mode === "review_connection") throw new Error("Observation interrupted")
          const [gpu, cpu, memory_gib] = af3.resource.quota.split(",").map(Number)
          return { name: mode === "review_identity" ? "different-job" : reserved.name,
            status: mode === "review_unknown" ? "UNKNOWN" : mode === "review_running" ||
              (mode === "review_changed" && calls.observations === 2) ? "RUNNING" : "SUCCEEDED",
            project: af3.resource.project, compute_group: af3.resource.group, workspace: af3.resource.workspace,
            resource: { gpu, cpu, memory_gib, nodes: 1 } }
        }
        calls.count++
        if (mode === "lost_read" && calls.count === 2) throw new Error("Lost observation, do not resubmit")
        if (mode === "changed" && calls.count === 2) await Bun.write(`${directory}/af3/engine.log`, "changed fixture")
        expect(args.slice(0, 2)).toEqual(["notebook", "exec"])
        // Map only the transport's filesystem root to this isolated directory.
        // The actual reader still checks unchanged plan bytes and all files;
        // restoring the namespace models a remote filesystem, not remote science.
        const encoded = args.at(-1)!.match(/'([A-Za-z0-9+/=]+)'$/)![1]
        const packet = JSON.parse(Buffer.from(encoded, "base64").toString())
        const local = Buffer.from(JSON.stringify({ ...packet, root: tmp.path })).toString("base64")
        const process = Bun.spawn(["bash", "-c", args.at(-1)!.replace(encoded, local)], { stdout: "pipe", stderr: "pipe" })
        const [output, error, returncode] = await Promise.all([new Response(process.stdout).text(), new Response(process.stderr).text(), process.exited])
        expect(error).toBe("")
        const value = JSON.parse(output)
        if (value.directory) value.directory = value.directory.replace(tmp.path, packet.root)
        return { output: JSON.stringify(value), returncode }
      } }
      const context = { directory: tmp.path, sessionID: mode === "wrong_session" ? "another-session" : parent.plan.session, abort: new AbortController().signal }
      if (mode === "wrong_session") {
        await expect(readRefinementFailure(data.store, parent, context, workflow.id, cli)).rejects.toThrow("another conversation")
        expect(calls.count).toBe(0)
        return
      }
      const result = await readRefinementFailure(data.store, parent, context, workflow.id, cli)
      expect(result.report_ready).toBe(false)
      expect(result.compute_restarted).toBe(false)
      expect(result.failure_evidence_ready).toBe(mode === "complete" || mode === "review")
      if (mode === "complete" || mode === "review") {
        expect('files' in result && result.files?.[0].plan_sha256).toBe(failurePacket(af3, parent).plan_sha256)
        expect('files' in result && result.files?.[0].files).toHaveLength(2)
        expect('unavailable' in result && result.unavailable).toEqual([{ task_id: covdock.id, reason: "NOT_SUBMITTED_NO_REMOTE_EXECUTION_CLAIM" }])
        expect(JSON.stringify(result)).not.toContain("NON-SCIENTIFIC LOCAL FAILURE FIXTURE")
        const summary = refinementFailureSummary(result)
        expect(summary).not.toHaveProperty("files")
        expect(summary).toHaveProperty("inventories.0.file_count", 2)
        expect(summary).toHaveProperty("inventories.0.inventory_sha256", digest('files' in result && result.files![0]))
        expect(JSON.stringify(summary).length).toBeLessThan(10000)
        if (mode === "review") expect(summary).toHaveProperty("termination", [
          { task_id: af3.id, remote_state: "succeeded", check: "before_files" },
          { task_id: af3.id, remote_state: "succeeded", check: "after_files" },
        ])
      }
      if (["changed", "lost_read"].includes(mode)) {
        const retained = data.store.db.query("SELECT receipt FROM refinement_failure_witness WHERE id=?").get(af3.id)
        expect(retained).toBeTruthy()
        const again = await readRefinementFailure(data.store, parent, context, workflow.id, cli)
        expect(again.failure_evidence_ready).toBe(mode === "lost_read")
        expect(data.store.db.query("SELECT receipt FROM refinement_failure_witness WHERE id=?").get(af3.id)).toEqual(retained)
      }
      if (mode === "running") expect(calls.count).toBe(0)
      if (mode.startsWith("review")) {
        expect(calls.observations).toBe(["review", "review_changed"].includes(mode) ? 2 : 1)
        expect(calls.count).toBe(["review", "review_changed"].includes(mode) ? 2 : 0)
        expect(result.scientific_quality_verified).toBe(false)
        expect(result.platform_files_published).toBe(false)
      }
      expect(digest({ core: data.store.stages(parent.id), tasks: data.store.list(), runs: runs.list(), workflow: workflows.get(workflow.id) })).toBe(before)
    } finally { data.store.close() }
  })
}

test("production runtime admission binds actual prerequisites without granting compute and defers GPU checks only to explicit startup", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const fixture = diagnosticFixture(data)
    const initial = workflowFixture(data)
    const source = "/inspire/schrodinger/psp-v6.4/covalent_docking.py"
    const assets = { ...initial.stages[0].assets, ...initial.stages[1].assets,
      ...Object.fromEntries(fixture.value.files.map((file) => [file.path, file.sha256])),
      [fixture.value.af3.cpu_parameters.loader_path]: "a".repeat(64), [source]: "a".repeat(64) }
    const plan = refinementWorkflow.parse({ ...initial, delivery: defaultRefinementDelivery(), stages: initial.stages.map((stage) => ({ ...stage,
      assets, runtime_admission: "cpu-before-approval-gpu-at-start-v1", quality_policy: defaultRefinementQuality,
      ...(stage.engine.name === "af3" ? { geometry_contract: "af3-geometry-evidence-v1" } : {}) })) })
    const value = { ...fixture.value, covdock: { ...fixture.value.covdock,
      entitlements: { ...fixture.value.covdock.entitlements, state: "PASSED", covdock_available: true, ligprep_available: true,
        source_path: source, source_sha256: "a".repeat(64), returncode: 0 },
      license: { ...fixture.value.covdock.license, state: "UNVERIFIED", returncode: 0, code: "COVDOCK_ENTITLEMENT_NOT_TESTED" } },
      blockers: ["AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED"] }
    const signal = new AbortController().signal
    const result = await admitRefinement(data.store, plan, signal, async (_parent, profile, nonce) => ({ ...value, profile_digest: digest(profile), nonce }))
    expect(result.state).toBe("PASSED")
    expect(result.gpu_verified).toBe(false)
    expect(result.compute_submitted).toBe(false)
    expect(refinementCommand(plan.stages[0], data.store.get(data.task.id)).shell).toContain("jax.devices")
    await expect(admitRefinement(data.store, plan, signal, async (_parent, profile, nonce) => ({ ...fixture.value, profile_digest: digest(profile), nonce })))
      .rejects.toThrow("VENDOR_LICENSE_PREREQUISITES_FAILED")
    const changed = refinementWorkflow.parse({ ...plan, stages: plan.stages.map((stage) => ({ ...stage, assets: { ...stage.assets, [source]: "b".repeat(64) } })) })
    await expect(admitRefinement(data.store, changed, signal, async (_parent, profile, nonce) => ({ ...value, profile_digest: digest(profile), nonce })))
      .rejects.toThrow("FROZEN_RUNTIME_ASSETS_CHANGED")
    await expect(admitRefinement(data.store, initial, signal)).rejects.toThrow("explicit startup")
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
    await runRefinements(data.store, signal) // No approved workflow => no CLI invocation.
    expect(refinementExecutions(data.store, data.store.get(data.task.id))).toEqual([])
  } finally { data.store.close() }
})

for (const mode of ["normal", "lost_reply", "candidate_results", "candidate_lost_reply", "corrupt_copy", "changed_report", "no_policy", "file_limit", "byte_limit",
  "inspect_trash", "inspect_version", "inspect_receipt", "inspect_missing", "inspect_bytes", "inspect_race", "inspect_source", "inspect_abort"] as const) {
  test(`refinement delivery uses actual native Result APIs and preserves bounded approval: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const data = await setup(tmp.path, "/never-invoked", "af3", false, session.id)
      try {
        // Explicit isolated local contracts/files, not scientific engine output.
        const af3 = qualityFixture(data, "af3")
        const covdock = qualityFixture(data, "covdock")
        const workflow = refinementWorkflow.parse({ ...workflowFixture(data), stages: [
          { ...af3.plan, finish_before: covdock.plan.finish_before }, covdock.plan],
          ...(mode === "no_policy" ? {} : { delivery: { ...defaultRefinementDelivery(),
            ...(!mode.startsWith('candidate_') ? { final_ranking: 'af3-strain-pareto-v1', candidate_results: undefined } : {}),
            ...(mode === "file_limit" ? { max_files: 3 } : {}),
            ...(mode === "byte_limit" ? { max_file_bytes: 100, max_total_bytes: 100 } : {}) } }) })
        af3.receipt.digest = digest(workflow.stages[0])
        const original = "EXPLICIT NON-SCIENTIFIC LOCAL DELIVERY FIXTURE\n"
        const sha256 = new Bun.CryptoHasher("sha256").update(original).digest("hex")
        const files = [af3.receipt, covdock.receipt] as const
        for (const file of files) for (const name of Object.keys(file.evidence.artifacts)) file.evidence.artifacts[name] = sha256
        const results = workflow.stages.map((stage, index) => evaluateRefinementQuality(stage, data.task, files[index]))
        const report = combineRefinementReport(workflow, data.task, [results[0], results[1]], [files[0], files[1]])
        const workflows = new RefinementWorkflows(data.refinements)
        workflows.create(workflow)
        workflows.approve(workflow.id, digest(workflow), { directory: tmp.path, sessionID: session.id,
          abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
        const runs = new RefinementRuns(data.refinements)
        workflow.stages.forEach((stage, index) => {
          runs.ensure(stage.id); runs.acquire(stage.id, "fixture"); const run = runs.reserve(stage.id, "fixture")
          runs.update(stage.id, "fixture", "VERIFYING", null, run.name)
          runs.complete(stage.id, "fixture", results[index]); runs.release(stage.id, "fixture")
        })
        const before = digest({ tasks: data.store.list(), core: data.store.stages(data.task.id), runs: runs.list(), approval: workflows.get(workflow.id) })
        const count = { reads: 0, posts: 0, downloads: 0, dropped: false }
        const inspection = { active: false, artifact: "", changed: false }
        const read: typeof readRefinementReport = async () => {
          count.reads++
          return { task_id: data.task.id, core_state: data.task.state, workflow_id: workflow.id, stages: [], report_ready: true,
            report: (mode === "changed_report" && count.reads > 1 || inspection.active && mode === "inspect_source")
              ? { ...report, interpretation: { ...report.interpretation, limitation: "Changed fixture evidence" } } : report,
            compute_restarted: false, platform_files_published: false, verification: "verified", verification_started_at: Date.now(),
            verified_at: Date.now(), delivery_note: "Fixture readback only" }
        }
        const actual = Server.internalFetch()
        const fetcher = async (request: Request) => {
          if (request.method === "POST") count.posts++
          const response = await actual(request)
          if (inspection.active && mode === "inspect_race" && new URL(request.url).pathname.endsWith("/raw") && !inspection.changed) {
            inspection.changed = true
            await ArtifactStore.trash(Instance.project.id, inspection.artifact)
          }
          if ((mode === "lost_reply" || mode === 'candidate_lost_reply' && count.posts === 8) && request.method === "POST" && !count.dropped) { count.dropped = true; throw new Error("Lost local fixture save response") }
          return response
        }
        const transport = () => ({ download: async (_task: unknown, _remote: string, destination: string) => {
          count.downloads++; await Bun.write(destination, mode === "corrupt_copy" ? "changed fixture" : original)
        } })
        const queue = new RefinementDelivery(data.store, new Publications(data.store, "http://openscience.internal", fetcher), transport, read,
          async (_report, sources) => {
            expect(sources).toHaveLength(7)
            // Only this boundary uses fixture data; the real Python processor is tested separately.
            return { 'report/candidate-results.csv': 'compound_id,standardized_smiles\nfixture,CC\n',
              'report/final-pareto.csv': 'compound_id,final_rank\nfixture,1\n',
              'report/final-pareto.json': '{"algorithm_version":"library-first-pareto-v2.0"}\n' }
          })
        const signal = new AbortController().signal
        if (mode === "no_policy") {
          await expect(queue.tick(workflow.id, signal)).rejects.toThrow("No independently approved")
          await expect(queue.inspect(workflow.id, data.task, signal)).rejects.toThrow("No independently approved")
          expect(approvedRefinementDeliveries(data.store)).toEqual([])
          expect(count).toEqual({ reads: 0, posts: 0, downloads: 0, dropped: false })
          return
        }
        expect(approvedRefinementDeliveries(data.store)).toEqual([workflow.id])
        expect(await queue.inspect(workflow.id, data.task, signal)).toMatchObject({ state: "NOT_STARTED", verification: "not_ready", files: [] })
        await queue.tick(workflow.id, signal)
        expect((await queue.inspect(workflow.id, data.task, signal)).verification).toBe("not_ready")
        if (mode === "lost_reply") {
          expect(queue.get(workflow.id)?.state).toBe("PAUSED")
          expect(count.posts).toBe(1)
          queue.resume(workflow.id, data.task)
        }
        await queue.tick(workflow.id, signal)
        if (mode === 'candidate_lost_reply') queue.resume(workflow.id, data.task)
        await queue.tick(workflow.id, signal)
        if (mode === 'lost_reply') await queue.tick(workflow.id, signal) // Nine files, four per tick, plus reconciliation.
        const status = refinementDeliveries(data.store, data.task)[0]
        if (["normal", "lost_reply", 'candidate_results', 'candidate_lost_reply'].includes(mode) || mode.startsWith("inspect_")) {
          expect(status.state).toBe("COMPLETE")
          expect(status.completed_files).toBe(mode.startsWith('candidate_') ? 10 : 9)
          expect(count.posts).toBe(mode.startsWith('candidate_') ? 10 : 9) // A lost publication reply is never reposted.
          expect(count.downloads).toBe(4)
          for (const file of status.files) {
            const stored = await ArtifactStore.get(Instance.project.id, file.artifact_id)
            expect(stored?.versionCount).toBe(1)
            const bytes = await ArtifactStore.read(Instance.project.id, file.artifact_id, file.version_id)
            expect(new Bun.CryptoHasher("sha256").update(await bytes!.content.arrayBuffer()).digest("hex")).toBe(file.sha256)
          }
          const rendered = refinementReportFiles(report)
          expect(rendered["refinement-report.md"]).toContain(report.candidates[0].compound_id)
          expect(rendered["refinement-report.csv"]).toContain("af3_mpae")
          expect(JSON.parse(rendered["refinement-report.json"])).toEqual(report)
          expect(refinementInventory(report)).toHaveLength(mode.startsWith('candidate_') ? 7 : 9)
          if (!mode.startsWith('candidate_')) expect(JSON.parse(rendered['final-pareto.json']).policy).toBe('af3-strain-pareto-v1')
          else expect(status.files.map((file) => file.name)).toContain('report/candidate-results.csv')
          const historical = { ...report }
          delete historical.final_ranking
          expect(refinementInventory(historical)).toHaveLength(7)
          expect(refinementReportFiles(historical)['final-pareto.json']).toBeUndefined()
          const frozen = () => digest({ queue: queue.get(workflow.id), files: data.store.db.query("SELECT * FROM refinement_delivery_files ORDER BY name").all(),
            events: data.store.db.query("SELECT * FROM events ORDER BY sequence").all() })
          const snapshot = frozen()
          const writes = { posts: count.posts, downloads: count.downloads }
          const verified = await queue.inspect(workflow.id, data.task, signal)
          expect(verified).toMatchObject({ state: "COMPLETE", verification: "verified", report_digest: digest(report),
            compute_submitted: false, platform_files_published: false, md_ready: false })
          expect(verified.files).toHaveLength(status.completed_files)
          expect(JSON.stringify(verified)).not.toContain("local_path")
          expect(JSON.stringify(verified)).not.toContain(tmp.path)
          await expect(queue.inspect(workflow.id, { ...data.task, digest: "0".repeat(64) }, signal)).rejects.toThrow("another task")
          expect(frozen()).toBe(snapshot)
          const file = status.files[0]
          inspection.artifact = file.artifact_id
          const retained = data.store.db.query("SELECT * FROM refinement_delivery_files WHERE workflow_id=? AND name=?")
            .get(workflow.id, file.name) as { receipt: string }
          const native = await ArtifactStore.read(Instance.project.id, file.artifact_id, file.version_id)
          const content = await native!.content.arrayBuffer()
          if (mode === "inspect_trash") await ArtifactStore.trash(Instance.project.id, file.artifact_id)
          if (mode === "inspect_version") await ArtifactStore.save({ projectID: Instance.project.id, sessionID: session.id,
            sourcePath: file.local_path, filename: native!.info.filename, kind: "file", content: new Blob(["Changed version fixture"]) })
          if (mode === "inspect_receipt") data.store.db.query("UPDATE refinement_delivery_files SET receipt=? WHERE workflow_id=? AND name=?")
            .run(JSON.stringify({ ...file, version_id: "ver_changed" }), workflow.id, file.name)
          if (mode === "inspect_missing") data.store.db.query("UPDATE refinement_delivery_files SET receipt=NULL WHERE workflow_id=? AND name=?")
            .run(workflow.id, file.name)
          if (mode === "inspect_bytes") await Bun.write(native!.content.name!, new Uint8Array(content.byteLength).fill(88))
          inspection.active = true
          if (mode === "inspect_abort") {
            const cancelled = new AbortController(); cancelled.abort()
            await expect(queue.inspect(workflow.id, data.task, cancelled.signal)).rejects.toThrow()
          } else if (mode.startsWith("inspect_")) {
            const changed = await queue.inspect(workflow.id, data.task, signal)
            expect(changed.verification).not.toBe("verified")
            expect(changed.files).toEqual([])
            expect(queue.get(workflow.id)?.state).toBe("COMPLETE") // Historical completion is never rewritten by a read.
          }
          inspection.active = false
          if (["inspect_trash", "inspect_race"].includes(mode)) await ArtifactStore.restore(Instance.project.id, file.artifact_id)
          if (["inspect_receipt", "inspect_missing"].includes(mode)) data.store.db.query("UPDATE refinement_delivery_files SET receipt=? WHERE workflow_id=? AND name=?")
            .run(retained.receipt, workflow.id, file.name)
          if (mode === "inspect_bytes") await Bun.write(native!.content.name!, content)
          if (mode !== "inspect_version") expect((await queue.inspect(workflow.id, data.task, signal)).verification).toBe("verified")
          expect({ posts: count.posts, downloads: count.downloads }).toEqual(writes)
          expect(frozen()).toBe(snapshot)
        } else {
          expect(status.state).toBe("PAUSED")
          expect(status.completed_files).toBeLessThan(7)
          if (["file_limit", "byte_limit"].includes(mode)) expect(count.posts).toBe(0)
        }
        expect(digest({ tasks: data.store.list(), core: data.store.stages(data.task.id), runs: runs.list(), approval: workflows.get(workflow.id) })).toBe(before)
      } finally { data.store.close() }
    } })
  })
}

test("refinement report refuses to replace missing executions with a completed core or compiled draft", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const context = { sessionID: data.plan.session, directory: tmp.path, abort: new AbortController().signal }
  try {
    const before = digest({ tasks: data.store.list(), stages: data.store.stages(data.task.id) })
    data.store.event(data.task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", workflow: workflowFixture(data) })
    expect(await readRefinementReport(data.store, data.store.get(data.task.id), context)).toMatchObject({
      report_ready: false, reason: "NO_REFINEMENT_WORKFLOW", platform_files_published: false, stages: [] })
    expect(data.store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_runs'").get()).toBeNull()
    const workflows = new RefinementWorkflows(data.refinements)
    const workflow = workflowFixture(data)
    workflows.create(workflow)
    expect(await readRefinementReport(data.store, data.store.get(data.task.id), context)).toMatchObject({
      report_ready: false, reason: "WORKFLOW_NOT_APPROVED" })
    workflows.approve(workflow.id, digest(workflow), { ...context, metadata: () => {}, ask: async () => {} })
    expect(await readRefinementReport(data.store, data.store.get(data.task.id), context)).toMatchObject({
      report_ready: false, reason: "REFINEMENT_NOT_EXECUTED" })
    const runs = new RefinementRuns(data.refinements)
    runs.ensure(workflow.stages[0].id)
    expect(await readRefinementReport(data.store, data.store.get(data.task.id), context)).toMatchObject({
      report_ready: false, reason: "BOTH_REFINEMENT_STAGES_MUST_COMPLETE",
      stages: [{ state: "WAITING", submitted_at: null }, { state: "NOT_STARTED", submitted_at: null }] })
    runs.acquire(workflow.stages[0].id, "report-test")
    runs.reserve(workflow.stages[0].id, "report-test")
    runs.update(workflow.stages[0].id, "report-test", "FAILED", "Retained fixture execution failure")
    runs.release(workflow.stages[0].id, "report-test")
    expect(await readRefinementReport(data.store, data.store.get(data.task.id), context)).toMatchObject({
      report_ready: false, stages: [{ state: "FAILED", error: "Retained fixture execution failure" }, { state: "NOT_STARTED" }] })
    await expect(readRefinementReport(data.store, data.store.get(data.task.id), { ...context, sessionID: "another" })).rejects.toThrow("another conversation")
    expect(digest({ tasks: data.store.list(), stages: data.store.stages(data.task.id) })).toBe(before)
    expect(screeningToolSchema.parse({ action: "refinement_report", task_id: data.task.id }).action).toBe("refinement_report")
  } finally { data.store.close() }
})

test("provisional report preserves independent axes through workflow projection and exported files", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const af3 = qualityFixture(data, "af3", true)
    const covdock = qualityFixture(data, "covdock", true)
    const workflow = refinementWorkflow.parse({ ...workflowFixture(data), stages: [
      { ...af3.plan, finish_before: covdock.plan.finish_before }, covdock.plan] })
    af3.receipt.digest = digest(workflow.stages[0])
    const incomplete = covdock.receipt.quality!.candidates[0].metrics!
    if (incomplete.engine !== "covdock") throw Error("Unexpected fixture")
    incomplete.strain_kcal_mol = null
    incomplete.strain_provenance!.status = "MISSING"
    const files = [af3.receipt, covdock.receipt] as const
    const outcomes = workflow.stages.map((stage, index) => evaluateRefinementQuality(stage, data.task, files[index]))
    const report = combineRefinementReport(workflow, data.task, [outcomes[0], outcomes[1]], [files[0], files[1]])
    expect(report.candidates[0].status).toBe("NOT_FULLY_EVALUATED")
    expect(report.candidates[1].status).toBe("BOTH_STRUCTURAL_QC_PASSED")
    const projected = projectRefinementReport(report)
    expect(projected.candidates[0].covdock.quality.assessment?.physical_validity).toBe("UNKNOWN")
    expect(projected.candidates[0].covdock.measurements).toEqual(incomplete)
    const rendered = refinementReportFiles(report)
    expect(rendered["refinement-report.csv"]).toContain("covdock_physical_validity")
    expect(rendered["refinement-report.md"]).toContain("UNKNOWN")
    expect(rendered["refinement-report.md"]).toContain("未准入不等于结构失败")
    const root = path.resolve(import.meta.dir, "../../../../../..")
    const python = Bun.spawnSync([process.env.COVSCREEN_TEST_PYTHON ?? "python3", "-B", "-c",
      "import json,sys; from covscreen_library_first.refinement_results import Report; Report.model_validate(json.load(sys.stdin)); print('valid')"],
      { env: { ...process.env, PYTHONPATH: [path.join(root, "science/src"),
        path.join(root, "routes/library-first/src")].join(path.delimiter) }, stdin: Buffer.from(JSON.stringify(projected)) })
    expect(python.stderr.toString()).toBe("")
    expect(python.exitCode).toBe(0)
    expect(python.stdout.toString().trim()).toBe("valid")
  } finally { data.store.close() }
})

test("combined refinement report reapplies real classifier and retains structural disagreements without affinity claims", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    // Explicit local contract fixtures only. No AF3 or Schrödinger computation.
    const af3 = qualityFixture(data, "af3")
    const covdock = qualityFixture(data, "covdock")
    const workflow = refinementWorkflow.parse({ ...workflowFixture(data), stages: [
      { ...af3.plan, finish_before: covdock.plan.finish_before }, covdock.plan] })
    af3.receipt.digest = digest(workflow.stages[0])
    const rejected = covdock.receipt.quality!.candidates[0].metrics!
    if (rejected.engine !== "covdock") throw new Error("Unexpected fixture")
    rejected.strain_kcal_mol = 30
    const files = [af3.receipt, covdock.receipt] as const
    const saved = workflow.stages.map((stage, index) => evaluateRefinementQuality(stage, data.task, files[index]))
    const report = combineRefinementReport(workflow, data.task, [saved[0], saved[1]], [files[0], files[1]])
    expect(report.candidate_count).toBe(2)
    expect(report.hits_confirmed).toBe(false)
    expect(report.affinity_validated).toBe(false)
    for (const [index, source] of workflow.stages[0].input.candidates.entries()) expect(report.candidates[index]).toMatchObject(source)
    expect(report.candidates[0]).toMatchObject({ status: "STRUCTURAL_QC_DISAGREEMENT",
      af3: { quality: { status: "PASSED" } }, covdock: { quality: { status: "REJECTED", reason: "ligand_strain_above_approved_gate" } } })
    expect(report.counts).toEqual({ BOTH_STRUCTURAL_QC_PASSED: 1, STRUCTURAL_QC_DISAGREEMENT: 1,
      BOTH_STRUCTURAL_QC_REJECTED: 0, NOT_FULLY_EVALUATED: 0 })
    expect(report.stages[0].artifacts[0].sha256).toBe(saved[0].artifacts["fixture-0.cif"])
    const projected = projectRefinementReport(report)
    expect(projected.report_digest).toBe(digest(report))
    expect(digest(projected.counts)).toBe(digest(report.counts))
    expect(projected.candidates.map(row => [row.compound_id, row.status, row.af3.measurements, row.covdock.measurements]))
      .toEqual(report.candidates.map(row => [row.compound_id, row.status, row.af3.measurements, row.covdock.measurements]))
    expect(JSON.stringify(projected)).not.toContain("remote_path")
    expect(JSON.stringify(projected)).not.toContain("fixture-0.cif")

    expect(() => combineRefinementReport(workflow, data.task, [{ ...saved[0], candidates: [] }, saved[1]], [files[0], files[1]])).toThrow()
    const changed = structuredClone(files[0])
    changed.quality!.candidates[0].metrics = { engine: "af3", mpae: 1, iptm: .7, ligand_plddt_mean: 75, has_clash: true }
    expect(() => combineRefinementReport(workflow, data.task, [saved[0], saved[1]], [changed, files[1]])).toThrow("Fresh structural evaluation differs")
    expect(() => combineRefinementReport(workflow, { ...data.task, digest: "0".repeat(64) }, [saved[0], saved[1]], [files[0], files[1]])).toThrow("another core")
  } finally { data.store.close() }
})

for (const mode of [false, true, "md"] as const) {
  const zero = mode === true
  test(`bound refinement report reuses actual CLI reader and classifier mode=${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const cli = path.join(tmp.path, "report.ts")
    await Bun.write(cli, `#!${process.execPath}\n` + await Bun.file(path.join(import.meta.dir, "refinement-report-cli.fixture.ts")).text())
    await chmod(cli, 0o700)
    const data = await setup(tmp.path, cli, "af3", true, session.id, true, mode === "md")
    try {
      const task = data.store.get(data.task.id)
      const af3 = qualityFixture(data, "af3")
      const covdock = qualityFixture(data, "covdock")
      const plan = refinementWorkflow.parse({ ...workflowFixture(data), stages: [
        { ...af3.plan, finish_before: covdock.plan.finish_before }, covdock.plan],
        ...(!zero ? { delivery: mode === "md" ? defaultRefinementDelivery() : { ...defaultRefinementDelivery(), final_ranking: "af3-strain-pareto-v1", candidate_results: undefined } } : {}) })
      af3.receipt.digest = digest(plan.stages[0])
      const original = "EXPLICIT LOCAL REPORT AND PUBLICATION FIXTURE; NO SCIENTIFIC COMPUTE\n".repeat(mode === "md" ? 5000 : 1)
      const sha256 = new Bun.CryptoHasher("sha256").update(original).digest("hex")
      if (mode === "md") {
        const name = "covdock_lo/library_first_core_subset_covdock_lo-out.maegz"
        covdock.receipt.evidence.artifacts = { [name]: sha256 }
        covdock.receipt.evidence.candidates.forEach((row, index) => { row.structure!.path = name; row.structure!.pose_index = index })
        covdock.receipt.quality!.candidates.forEach(row => { row.evidence = [name] })
      }
      for (const file of [af3.receipt, covdock.receipt]) for (const name of Object.keys(file.evidence.artifacts)) file.evidence.artifacts[name] = sha256
      for (const [index, row] of covdock.receipt.quality!.candidates.entries()) {
        if (row.metrics?.engine !== "covdock") throw Error("Wrong fixture")
        if (zero || (mode !== "md" && index === 0)) row.metrics.strain_kcal_mol = 30
      }
      if (zero) for (const row of af3.receipt.quality!.candidates) {
        if (row.metrics?.engine !== "af3") throw Error("Wrong fixture")
        row.metrics.has_clash = true
      }
      const selected = proposalFixture(data)
      const planning = refinementPlanningScope.parse({ schema: "bound-refinement-planning-v1", operation_id: "e".repeat(32),
        parent: data.reference, selection: { selection_id: selected.selection_id, selection_manifest_digest: selected.selection_manifest_digest,
          policy: selected.policy, reason: "Software fixture with exact selection and report binding" },
        max_points: plan.max_points, scientific_review: "Software report test, no biological validation or real compute" })
      const reference = { planning_scope: planning, workflow_id: plan.id, digest: digest(plan) }
      retainPlanningEvent(data.store, task.id, "refinement_workflow_draft",
        { state: "COMPILED_NOT_EXECUTABLE", workflow: plan, digest: digest(plan) }, planning)
      const workflows = new RefinementWorkflows(data.refinements)
      workflows.create(plan)
      workflows.approve(plan.id, digest(plan), { directory: tmp.path, sessionID: task.plan.session,
        abort: new AbortController().signal, metadata() {}, async ask() { throw Error("No new approval") } })
      await expect(resumeWorkflowRefinementDelivery(data.store, task, reference,
        { directory: tmp.path, sessionID: task.plan.session }, new AbortController().signal,
        async () => { throw Error("Must not start an observer before completion") })).rejects.toThrow("both approved completed")
      const runs = new RefinementRuns(data.refinements)
      new RefinementPreparation(runs, new Inspire(task.plan.connection))
      const files = [af3.receipt, covdock.receipt]
      const stages = Object.fromEntries(plan.stages.map((stage, index) => {
        const staging = stagingFixture({ ...data, plan: stage })
        data.store.db.query("INSERT INTO refinement_preparation VALUES(?,?,?,?)").run(stage.id, digest(stage), JSON.stringify(staging), Date.now())
        runs.ensure(stage.id); runs.acquire(stage.id, "fixture"); const run = runs.reserve(stage.id, "fixture")
        runs.update(stage.id, "fixture", "VERIFYING", null, run.name)
        runs.complete(stage.id, "fixture", evaluateRefinementQuality(stage, task, files[index])); runs.release(stage.id, "fixture")
        return [stage.id, { staging, readback: files[index] }]
      }))
      const config = { parent: task.id, digest: task.digest, stages,
        core: Object.fromEntries(data.store.stages(task.id).map(stage => [stage.phase, JSON.parse(stage.manifest!)])) }
      await Bun.write(cli + ".json", JSON.stringify(config))
      const scope = { directory: tmp.path, sessionID: task.plan.session }
      const calls = { posts: 0, readonly: false, drop: false }
      const actual = Server.internalFetch()
      const publications = new Publications(data.store, "http://openscience.internal", async request => {
        if (request.method !== "GET") {
          if (calls.readonly) throw Error("Report reads must never publish")
          calls.posts++
        }
        const response = await actual(request)
        if (calls.drop && request.method === "POST") { calls.drop = false; throw Error("Lost native publication response") }
        return response
      })
      const queue = new RefinementDelivery(data.store, publications,
        () => ({ download: async (_task, _remote, destination) => { await Bun.write(destination, original) } }), undefined,
        mode === "md" ? async (report, sources) => {
          const structure = sources.find(row => row.name === "covdock/covdock_lo/library_first_core_subset_covdock_lo-out.maegz")!
          const columns = ["route", "task_id", "result_scope", "online_structural_status", "compound_id", "input_sdf_index",
            "standardized_smiles", "warhead_class", "covdock_structure_pose", "covdock_artifact_id", "covdock_artifact_version", "covdock_artifact_sha256"]
          const rows = report.candidates.map((row, index) => ["library-first", report.workflow_id, "candidate-results-v1", row.status,
            row.compound_id, row.input_sdf_index, "C=CC(=O)NC", "acrylamide", index, structure.artifact_id, structure.version_id, structure.sha256])
          const csv = [columns, ...rows].map(row => row.map(value => JSON.stringify(String(value))).join(",")).join("\n") + "\n"
          return { "report/candidate-results.csv": csv, "report/final-pareto.csv": csv,
            "report/final-pareto.json": JSON.stringify({ software_fixture: true }) }
        } : undefined)
      const read = () => readWorkflowRefinementResults(data.store, task, reference, scope, new AbortController().signal, queue)
      const result = await read()
      expect(result.result.verification).toBe("verified")
      expect(result.result.report?.candidate_count).toBe(mode === "md" ? 1 : 2)
      expect(result.result.report?.counts.BOTH_STRUCTURAL_QC_PASSED).toBe(zero ? 0 : 1)
      expect(result.result.report?.counts.STRUCTURAL_QC_DISAGREEMENT).toBe(zero || mode === "md" ? 0 : 1)
      expect((await read()).result.report).toEqual(result.result.report)
      const choice = mdCandidateChoice.parse({ schema: "library-md-candidate-choice-v1", id: "md-choice-" + "a".repeat(32),
        candidate: { compound_id: task.id, input_sdf_index: 1 }, reason: "Software context binding test, no biological claim",
        native_workflow_id: plan.id, native_workflow_digest: digest(plan), task_id: task.id,
        report_digest: result.result.report!.report_digest, selected_at: Date.now(),
        files: Object.fromEntries(["candidate_table", "covdock_structure"].map(name => [name, {
          name, artifact_id: "art_" + name, version_id: "ver_" + name, project_id: Instance.project.id, sha256, bytes: original.length }])),
        parents: { preparation_run_id: "software-preparation", prepared_receptor_manifest_sha256: "b".repeat(64),
          receptor_context: { id: "receptor-context-" + "c".repeat(32), sha256: "d".repeat(64) } } })
      const native = readMdNativeTarget(data.store, task, reference, choice)
      expect(native.parameters).toEqual({ chain: "A", residue: "CYS", residue_number: 797, reaction_type: "Michael Addition" })
      expect(native.native_workflow_digest).toBe(digest(plan))
      expect(native.artifact_sha256).toBe(choice.files.covdock_structure.sha256)
      expect(() => readMdNativeTarget(data.store, task, reference, { ...choice, native_workflow_digest: "0".repeat(64) })).toThrow("exact approved")
      if (mode === "md") {
        for (const _ of [0, 1, 2, 3]) await queue.tick(plan.id, new AbortController().signal)
        const delivered = await read()
        expect(delivered.result.delivery?.verification).toBe("verified")
        calls.readonly = true
        const research = data.research!
        const hostScope = { project: Instance.project.id, ...scope, ledger_id: workflowLedgerIdentity(data.store) }
        const selection = { selection_id: selected.selection_id, selection_manifest_digest: selected.selection_manifest_digest,
          source_manifest_digest: selected.source_manifest_digest, input_sha256: selected.input.sha256, identity_sha256: selected.input.identity_sha256,
          policy: selected.policy, reason: planning.selection.reason, status: selected.status, input_count: selected.input_count,
          eligible_count: selected.eligible_count, excluded_count: selected.excluded_count, selected_count: selected.selected_count,
          effective_cap: selected.effective_cap, candidates: selected.input.candidates, calibrated_uncertainty: false as const,
          refinement_approval_required: true as const, limitations: selected.limitations }
        await exerciseMdHost({ directory: tmp.path, store: data.store, publications, scope: hostScope,
          intent: research.intent, source: research.environment.source, reference, selection, workflow: plan,
          core: workflowObservation(data.store, data.reference!, scope), report: delivered.result, observation: delivered.observation })
      } else if (!zero) {
        expect(result.result.delivery).toMatchObject({ state: "NOT_STARTED", verification: "not_ready", files: [] })
        const signal = new AbortController().signal
        const forbidden = async () => { throw Error("Unexpected observer startup") }
        await expect(resumeWorkflowRefinementDelivery(data.store, task, reference, { ...scope, sessionID: "another" }, signal, forbidden))
          .rejects.toThrow("another conversation")
        await expect(resumeWorkflowRefinementDelivery(data.store, task, { ...reference, digest: "0".repeat(64) }, scope, signal, forbidden))
          .rejects.toThrow("bound plan")
        const cancelled = new AbortController(); cancelled.abort()
        await expect(resumeWorkflowRefinementDelivery(data.store, task, reference, scope, cancelled.signal, forbidden)).rejects.toThrow()
        const frozen = digest({ runs: runs.list(), approval: workflows.get(plan.id).approval })
        calls.drop = true
        expect(await resumeWorkflowRefinementDelivery(data.store, task, reference, scope, signal,
          async () => { await queue.tick(plan.id, signal) })).toBe(true)
        expect(queue.get(plan.id)?.state).toBe("PAUSED")
        expect(calls.posts).toBe(1)
        const attempts = digest(data.store.db.query("SELECT * FROM refinement_delivery_files ORDER BY name").all())
        await expect(resumeWorkflowRefinementDelivery(data.store, task, reference, scope, signal,
          async () => { throw Error("Observer temporarily unavailable") })).rejects.toThrow("temporarily unavailable")
        expect(queue.get(plan.id)?.state).toBe("WAITING")
        expect(digest(data.store.db.query("SELECT * FROM refinement_delivery_files ORDER BY name").all())).toBe(attempts)
        expect(await resumeWorkflowRefinementDelivery(data.store, task, reference, scope, signal,
          async () => { for (const _ of [0, 1, 2]) await queue.tick(plan.id, signal) })).toBe(true)
        expect(await resumeWorkflowRefinementDelivery(data.store, task, reference, scope, signal, forbidden)).toBe(false)
        expect(digest({ runs: runs.list(), approval: workflows.get(plan.id).approval })).toBe(frozen)
        calls.readonly = true
        const delivered = await read()
        expect(delivered.result.delivery).toMatchObject({ verification: "verified", state: "COMPLETE",
          workflow_id: reference.workflow_id, workflow_digest: reference.digest, report_digest: result.result.report!.report_digest })
        expect(delivered.result.delivery!.files).toHaveLength(9)
        expect(calls.posts).toBe(9)
        const file = delivered.result.delivery!.files[0]
        await ArtifactStore.trash(Instance.project.id, file.artifact_id)
        expect((await read()).result).toMatchObject({ verification: "verified", delivery: { verification: "evidence_rejected", files: [] } })
        await ArtifactStore.restore(Instance.project.id, file.artifact_id)
        expect((await read()).result.delivery!.files).toEqual(delivered.result.delivery!.files)
        expect(calls.posts).toBe(9)
      } else {
        expect(result.result.delivery).toBeNull() // Historical grants never gain a default delivery policy.
        await expect(resumeWorkflowRefinementDelivery(data.store, task, reference, scope, new AbortController().signal,
          async () => { throw Error("No observer without an approved delivery policy") })).rejects.toThrow("No independently approved")
        expect(calls.posts).toBe(0)
      }
      await Bun.write(cli + ".json", JSON.stringify({ ...config, unavailable: true }))
      expect((await read()).result).toMatchObject({ verification: "verification_unavailable", report: null })
      await Bun.write(cli + ".json", JSON.stringify({ ...config, changed: true }))
      expect((await read()).result).toMatchObject({ verification: "evidence_rejected", report: null })
      await Bun.write(cli + ".json", JSON.stringify(config))
      expect((await read()).result.report).toEqual(result.result.report)
      expect(data.store.db.query("SELECT * FROM refinement_workflows").all()).toHaveLength(1)
      expect(data.store.db.query("SELECT * FROM refinement_runs").all()).toHaveLength(2)
      expect(plan.stages.map(stage => runs.get(stage.id).lease_owner)).toEqual([null, null])
    } finally { data.store.close() }
    } })
  }, 60000)
}

test("host compiler binds both exact engine plans to a witnessed proposal without creating approval or execution", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const task = data.store.get(data.task.id)
    const fixture = diagnosticFixture(data)
    const prefix = `${task.plan.runtime.root}/releases/${data.plan.code.release_id}/`
    const names = [fixture.profile.af3, fixture.profile.covdock].flatMap((runtime) =>
      refinementPaths(runtime, task.plan.runtime.root, data.plan.code.release_id).files)
      .filter((file) => file.startsWith(prefix)).map((file) => file.slice(prefix.length))
    const inventory = { version: "screening-code-release-v1", files: Object.fromEntries(names.map((name) => [name, "a".repeat(64)])) }
    const code = { release_id: digest(inventory), manifest_sha256: new Bun.CryptoHasher("sha256")
      .update(JSON.stringify(canonical(inventory)) + "\n").digest("hex"), checker_sha256: "a".repeat(64) }
    const profile = refinementProfile.parse({ ...fixture.profile, code })
    await diagnoseRefinement(data.store, task.id, new AbortController().signal, async () => profile,
      async (_task, _profile, nonce) => ({ ...fixture.value, code, profile_digest: digest(profile), nonce }))
    const proposal = refinementProposal(data.store, task, { ...proposalFixture(data), policy: refinementSelectionPolicy },
      { af3: task.plan.steps[1].resource, covdock: task.plan.steps[0].resource, recorded_at: 1000 }, 1000)
    data.store.event(task.id, "refinement_execution_proposal", { state: "AVAILABLE", version: refinementProposalVersion,
      value: proposal, digest: digest(proposal) })
    const source = { text: data.plan.request, origin: data.plan.origin }
    const before = digest({ tasks: data.store.list(), stages: data.store.stages(task.id),
      refinements: data.store.db.query("SELECT * FROM refinements").all(), events: data.store.db.query("SELECT * FROM events").all() })
    const built = buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile, inventory, 1001)
    const [af3, covdock] = built.workflow.stages
    expect(built.digest).toBe(digest(built.workflow))
    expect(built.workflow.delivery).toEqual(defaultRefinementDelivery())
    expect(built.ready_for_approval).toBe(false)
    expect(built.compute_submitted).toBe(false)
    expect(built.blockers).toContain("INVALID_LICENSE_SYNTAX")
    expect(af3.input).toEqual(proposal.selection.input)
    expect(covdock.input).toEqual(af3.input)
    expect(af3.engine).toMatchObject({ name: "af3", num_diffusion_samples: 5, cpu_per_search: 2 })
    expect(digest(covdock.engine)).toBe(digest({ name: "covdock", ...proposal.stages[1].parameters }))
    expect(af3.code).toEqual({ release_id: code.release_id, manifest_sha256: code.manifest_sha256 })
    expect(af3.assets["/inspire/models/af3.bin"]).toBe("a".repeat(64))
    expect(af3.assets["/inspire/license.lic"]).toBe("a".repeat(64))
    expect(af3.quality_policy).toEqual(provisionalRefinementQuality)
    expect(covdock.quality_policy).toEqual(af3.quality_policy)
    expect(af3.geometry_contract).toBe("af3-geometry-evidence-v1")
    expect(covdock.geometry_contract).toBeUndefined()
    expect(af3.minutes).toBe(120)
    expect(covdock.minutes).toBe(420)
    expect(af3.finish_before - af3.approve_before).toBe(600 * 60000)
    expect(built.workflow.max_points).toBe(2)
    built.workflow.stages.forEach((stage) => expect(data.refinements.parent(stage).id).toBe(task.id))
    expect(() => buildRefinementWorkflow(data.store, task.id, "0".repeat(64), source, profile, inventory, 1001)).toThrow("proposal changed")
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile, inventory, 301001)).toThrow("stale resource")
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), { ...source, text: "changed" }, profile, inventory, 1001)).toThrow("provenance")
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), source,
      { ...profile, project: "other" }, inventory, 1001)).toThrow("witnessed environment")
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile,
      { ...inventory, files: { ...inventory.files, "new.py": "b".repeat(64) } }, 1001)).toThrow("manifest differs")
    expect(digest({ tasks: data.store.list(), stages: data.store.stages(task.id),
      refinements: data.store.db.query("SELECT * FROM refinements").all(), events: data.store.db.query("SELECT * FROM events").all() })).toBe(before)
    expect(refinementDraftSnapshot(data.store, task.id, 1001)).toBeNull()
    data.store.event(task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...built })
    expect(refinementDraftSnapshot(data.store, task.id, 1001)).toMatchObject({ state: "COMPILED_NOT_EXECUTABLE",
      id: built.workflow.id, ready_for_approval: false, compute_submitted: false })
    const checks = { workflow_digest: built.digest, checked_at: 1001, stages: built.workflow.stages.map((plan) => {
      const paths = refinementPaths(plan.runtime, task.plan.runtime.root, plan.code.release_id)
      const helper = `${paths.directory}/src/covscreen/refinement_input.py`
      return { engine: plan.engine.name, plan_digest: digest(plan), state: "PASSED", evidence: {
        engine: plan.engine.name, entry: paths.entry, entry_sha256: plan.assets[paths.entry], helper, helper_sha256: plan.assets[helper],
        missing_options: [], exact_input_guard_present: true, passes_static_checks: true, blocking_reasons: [],
        runtime_verified: false, license_checkout_verified: false, resource_approval_granted: false, code: plan.code,
        input_score_sources: ["core"], geometry_contracts: ["af3-geometry-evidence-v1"], original_structure_readback_supported: true,
        quality_policies: ["refinement-structural-qc-v1", "refinement-structural-qc-v2-provisional"] } }
    }) }
    expect(verifyWorkflowInterfaces(checks, built.workflow, task).stages).toHaveLength(2)
    expect(() => verifyWorkflowInterfaces({ ...checks, stages: [...checks.stages].reverse() }, built.workflow, task)).toThrow("identity changed")
    expect(() => verifyWorkflowInterfaces({ ...checks, stages: checks.stages.map((stage) => ({ ...stage,
      evidence: { ...stage.evidence, quality_policies: [] } })) }, built.workflow, task)).toThrow("QC measurement")
    expect(() => verifyWorkflowInterfaces({ ...checks, stages: checks.stages.map(stage => ({ ...stage,
      evidence: { ...stage.evidence, quality_policies: ["refinement-structural-qc-v1"] } })) }, built.workflow, task))
      .toThrow("QC measurement")
    data.store.event(task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...built, interface_checks: checks })
    const checked = refinementDraftSnapshot(data.store, task.id, 1001)
    if (!checked || !("blockers" in checked)) throw new Error("Missing checked draft")
    expect(checked.blockers).not.toContain("FROZEN_RELEASE_REFINEMENT_INTERFACE_NOT_VERIFIED")
    expect(checked.blockers).toContain("INVALID_LICENSE_SYNTAX")
    expect(checked.ready_for_approval).toBe(false)
    expect(checked.interface_checks?.runtime_verified).toBe(false)
    const args = refinementInterfaceArgs(af3, task)
    expect(args.slice(0, 7)).toEqual(["notebook", "exec", "fixture", "--workspace", "CPU资源空间", "--timeout", "60"])
    expect(args.at(-1)).toContain("def inspect_release")
    expect(args.at(-1)).not.toContain("&& source")
    const unavailable = await inspectWorkflowInterfaces(built.workflow, task, new Inspire(task.plan.connection))
    expect(unavailable.stages.map((stage) => stage.state)).toEqual(["UNAVAILABLE", "UNAVAILABLE"])
    expect(refinementDraftSnapshot(data.store, task.id, af3.approve_before + 1)?.state).toBe("EXPIRED")
    const changed = { ...built.workflow, stages: built.workflow.stages.map((stage) => ({ ...stage, minutes: stage.minutes + 1, max_points: stage.max_points + 1 })),
      max_points: built.workflow.max_points + 2 }
    data.store.event(task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...built, workflow: changed, digest: digest(changed) })
    expect(refinementDraftSnapshot(data.store, task.id, 1001)?.state).toBe("UNAVAILABLE")
    data.store.event(task.id, "refinement_workflow_draft", { state: "UNAVAILABLE" })
    expect(refinementDraftSnapshot(data.store, task.id, 1001)?.state).toBe("UNAVAILABLE")
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

test("bound research planning preserves the reviewed subset and cap across concurrent retention, newer proposals and restart", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path, "/never-invoked", "af3", false, "bound-research", true)
  const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
  try {
    const task = data.store.get(data.task.id)
    const selected = proposalFixture(data)
    const scope = refinementPlanningScope.parse({ schema: "bound-refinement-planning-v1", operation_id: "e".repeat(32),
      parent: data.reference, selection: { selection_id: selected.selection_id, selection_manifest_digest: selected.selection_manifest_digest,
        policy: selected.policy, reason: "Retain the explicitly reviewed disagreement and consensus candidates" },
      max_points: 2, scientific_review: "Review the same subset independently with AF3 and CovDock; no activity claim" })
    const rates = { af3: task.plan.steps[1].resource, covdock: task.plan.steps[0].resource, recorded_at: 1000 }
    const diagnostic = diagnosticFixture(data)
    const prefix = `${task.plan.runtime.root}/releases/${data.plan.code.release_id}/`
    const names = [diagnostic.profile.af3, diagnostic.profile.covdock].flatMap(runtime =>
      refinementPaths(runtime, task.plan.runtime.root, data.plan.code.release_id).files)
      .filter(file => file.startsWith(prefix)).map(file => file.slice(prefix.length))
    const inventory = { version: "screening-code-release-v1", files: Object.fromEntries(names.map(name => [name, "a".repeat(64)])) }
    const code = { release_id: digest(inventory), manifest_sha256: new Bun.CryptoHasher("sha256")
      .update(JSON.stringify(canonical(inventory)) + "\n").digest("hex"), checker_sha256: "a".repeat(64) }
    const profile = refinementProfile.parse({ ...diagnostic.profile, code })
    await diagnoseRefinement(data.store, task.id, new AbortController().signal, async () => profile,
      async (_task, _profile, nonce) => ({ ...diagnostic.value, code, profile_digest: digest(profile), nonce }))
    const proposal = refinementProposal(data.store, task, selected, rates, 1000, scope)
    expect(proposal.version).toBe(boundRefinementProposalVersion)
    expect(proposal.selection.policy).toEqual(selected.policy)
    expect(proposal.selection.policy).not.toEqual(refinementSelectionPolicy)
    expect(proposal.selection.input).toEqual(data.plan.input)
    expect(proposal.planning_scope).toEqual(scope)
    expect(proposal.resource_approval_granted).toBe(false)
    const payload = { state: "AVAILABLE", version: proposal.version, value: proposal, digest: digest(proposal) }
    retainPlanningEvent(data.store, task.id, "refinement_execution_proposal", payload, scope)
    retainPlanningEvent(reopened, task.id, "refinement_execution_proposal", { state: "UNAVAILABLE" }, scope)
    expect(refinementProposalSnapshot(reopened, reopened.get(task.id), scope)?.proposal).toEqual(proposal)
    expect(() => planningEvent(reopened, task.id, "refinement_execution_proposal", { ...scope, max_points: 9 })).toThrow("different candidates")
    const source = { text: data.plan.request, origin: data.plan.origin }
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile, inventory, 1001)).toThrow("exact research")
    const built = buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile, inventory, 1001, scope)
    expect(built.workflow.max_points).toBe(2)
    expect(built.workflow.stages.every(stage => digest(stage.input) === digest(selected.input))).toBe(true)
    retainPlanningEvent(data.store, task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...built }, scope)
    const receipt = refinementPlanningObservation(data.store, task, scope, 1001)
    expect(receipt).toMatchObject({ planning_scope: scope, compute_submitted: false, approval_recorded: false,
      proposal: { digest: digest(proposal), selected_count: selected.selected_count },
      draft: { workflow_id: built.workflow.id, digest: built.digest, state: "COMPILED_NOT_EXECUTABLE" } })
    expect(JSON.stringify(receipt)).not.toContain("/inspire/")
    expect(refinementPlanningObservation(data.store, task, scope).draft?.state).toBe("EXPIRED")
    const other = { ...scope, operation_id: "f".repeat(32) }
    const later = refinementProposal(data.store, task, selected, rates, 1000, other)
    retainPlanningEvent(reopened, task.id, "refinement_execution_proposal", { ...payload, value: later, digest: digest(later) }, other)
    expect(refinementProposalSnapshot(data.store, task)?.proposal?.planning_scope?.operation_id).toBe(other.operation_id)
    expect(refinementProposalSnapshot(data.store, task, scope)?.proposal).toEqual(proposal)
    expect(refinementDraftSnapshot(data.store, task.id, 1001, scope)).toMatchObject({ id: built.workflow.id, digest: built.digest, planning_scope: scope })
    // Both recovery calls return before environment access; /never-invoked
    // cannot execute a command. Expiry never silently generates a new plan.
    expect((await planRefinement(reopened, reopened.get(task.id), new AbortController().signal, scope))?.proposal).toEqual(proposal)
    expect(await prepareRefinementDraft(reopened, task.id, source, scope)).toMatchObject({ id: built.workflow.id, state: "EXPIRED" })
    await expect(prepareRefinementDraft(reopened, task.id, { ...source, text: "Substituted original request" }, scope)).rejects.toThrow("original request")
    const context = { directory: tmp.path, sessionID: task.plan.session, abort: new AbortController().signal,
      metadata() {}, async ask() { throw Error("No native approval may be reached by this identity test") } }
    await expect(approveRefinementExecution(data.store, task, context)).rejects.toThrow("exact research approval reference")
    await expect(approveRefinementExecution(data.store, task, context, { planning_scope: scope,
      workflow_id: built.workflow.id, digest: "0".repeat(64) })).rejects.toThrow("draft changed")
    await expect(approveRefinementExecution(data.store, task, context, { planning_scope: scope,
      workflow_id: built.workflow.id, digest: built.digest })).rejects.toThrow("fresh platform refinement plan")
    const low = { ...scope, operation_id: "1".repeat(32), max_points: 1 }
    const excessive = refinementProposal(data.store, task, selected, rates, 1000, low)
    expect(excessive.blockers).toContain("PROPOSED_REFINEMENT_BUDGET_EXCEEDED")
    retainPlanningEvent(data.store, task.id, "refinement_execution_proposal", { ...payload, value: excessive, digest: digest(excessive) }, low)
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(excessive), source, profile, inventory, 1001, low)).toThrow("independent budget")
    expect(() => refinementProposal(data.store, task, { ...selected, selection_id: "0".repeat(64) }, rates, 1000, scope)).toThrow()
    expect(() => refinementProposal(data.store, task, selected, rates, 1000,
      { ...scope, parent: { ...scope.parent, plan_sha256: "0".repeat(64) } })).toThrow()
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
    expect(data.store.db.query("SELECT * FROM refinement_workflows").all()).toEqual([])
    expect(data.store.get(task.id).approval).toBe(task.approval)
    const otherBuilt = buildRefinementWorkflow(data.store, task.id, digest(later), source, profile, inventory, 1001, other)
    retainPlanningEvent(data.store, task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...otherBuilt }, other)
    new RefinementWorkflows(data.refinements).create(otherBuilt.workflow)
    expect(() => closeRefinementPlanning(data.store, task, other, "Must not release a native confirmation")).toThrow("confirmation exists")
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(proposal), source, profile, inventory, 1001, scope)).toThrow("existing refinement")
    const closure = closeRefinementPlanning(data.store, task, scope, "Release this exact unactivated research planning operation")
    expect(readRefinementPlanningClosure(reopened, task.id, scope)).toEqual(closure)
    expect(closeRefinementPlanning(reopened, reopened.get(task.id), scope, "A later reason cannot replace the original closure")).toEqual(closure)
    expect(() => retainPlanningEvent(reopened, task.id, "refinement_workflow_draft", { state: "UNAVAILABLE" }, scope)).toThrow("closed")
    expect(() => new RefinementWorkflows(data.refinements).create(built.workflow)).toThrow("Released refinement")
    await expect(approveRefinementExecution(data.store, task, context, { planning_scope: scope,
      workflow_id: built.workflow.id, digest: built.digest })).rejects.toThrow("closed")
  } finally { data.store.close(); reopened.close() }
})

for (const mode of ["pending", "rejected", "recover", "cancel", "complete", "tamper", "foreign", "observer_failure", "unknown", "changed_parent"] as const) {
  test(`bound refinement lifecycle verifies native records and never replaces execution: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path, "/never-invoked", "af3", true, "bound-lifecycle", true)
    try {
      const task = data.store.get(data.task.id)
      const plan = workflowFixture(data)
      const selected = proposalFixture(data)
      const scope = refinementPlanningScope.parse({ schema: "bound-refinement-planning-v1", operation_id: "e".repeat(32),
        parent: data.reference, selection: { selection_id: selected.selection_id, selection_manifest_digest: selected.selection_manifest_digest,
          policy: selected.policy, reason: "Software fixture preserving the exact reviewed candidate subset" },
        max_points: plan.max_points, scientific_review: "Software test of native approval provenance and exact workflow recovery" })
      retainPlanningEvent(data.store, task.id, "refinement_workflow_draft",
        { state: "COMPILED_NOT_EXECUTABLE", workflow: plan, digest: digest(plan) }, scope)
      const reference = { planning_scope: scope, workflow_id: plan.id, digest: digest(plan) }
      expect(readRefinementObservation(data.store, task, reference).native_state).toBe("NOT_CREATED")
      const uncreated = await readWorkflowRefinementResults(data.store, task, reference,
        { directory: tmp.path, sessionID: task.plan.session }, new AbortController().signal)
      expect(uncreated.result).toMatchObject({ verification: "not_ready", report: null, compute_submitted: false })
      expect(uncreated.observation.native_state).toBe("NOT_CREATED")
      await expect(readWorkflowRefinementResults(data.store, task, { ...reference, digest: "f".repeat(64) },
        { directory: tmp.path, sessionID: task.plan.session }, new AbortController().signal)).rejects.toThrow()
      const workflows = new RefinementWorkflows(data.refinements)
      workflows.create(plan)
      const context = { directory: tmp.path, sessionID: task.plan.session, abort: new AbortController().signal,
        metadata() {}, async ask() { throw Error("Software recovery must never request a new approval") } }
      const wakes: string[] = []
      const observer = async () => { wakes.push(mode); if (mode === "observer_failure") throw Error("Fixture observer unavailable") }
      if (["pending", "rejected"].includes(mode)) {
        expect(readRefinementObservation(data.store, task, reference)).toMatchObject({ native_state: "DRAFT", approval_recorded: false, execution_state: "not_submitted" })
        if (mode === "pending") await expect(advanceNativeRefinement(data.store, task, "approve", context, reference, observer)).rejects.toThrow("pending")
        await advanceNativeRefinement(data.store, task, "cancel", context, reference, observer)
        expect(readRefinementObservation(data.store, task, reference).native_state).toBe("CANCELLED")
        expect(closeRefinementPlanning(data.store, task, scope, "Release cancelled unapproved fixture").state).toBe("NO_EXECUTABLE_WORKFLOW")
        expect(data.store.db.query("SELECT * FROM refinement_runs").all()).toEqual([])
        return
      }
      // Native approval fixture only; no permission prompt or remote compute.
      workflows.approve(plan.id, digest(plan), context)
      const grant = workflows.get(plan.id).approval
      expect(readRefinementObservation(data.store, task, reference)).toMatchObject({ native_state: "APPROVED", approval_recorded: true, execution_state: "not_submitted" })
      if (mode === "foreign") {
        await expect(advanceNativeRefinement(data.store, task, "cancel", { ...context, sessionID: "other" }, reference, observer)).rejects.toThrow("conversation")
        expect(wakes).toEqual([])
        return
      }
      if (mode === "recover" || mode === "observer_failure") {
        const result = advanceNativeRefinement(data.store, task, "approve", context, reference, observer)
        if (mode === "observer_failure") await expect(result).rejects.toThrow("observer unavailable")
        else { expect(await result).toMatchObject({ workflow_id: plan.id, digest: digest(plan) }); await advanceNativeRefinement(data.store, task, "wake", context, reference, observer) }
      }
      const runs = new RefinementRuns(data.refinements)
      if (mode === "changed_parent") {
        const stage = data.store.stages(task.id)[3]
        const manifest = JSON.parse(stage.manifest!)
        manifest.artifacts["fixture.txt"] = "f".repeat(64)
        data.store.stage(task.id, stage.ordinal, { manifest: JSON.stringify(manifest) })
        expect(() => readRefinementObservation(data.store, task, reference)).toThrow("different core report")
        return
      }
      if (mode === "unknown") {
        const stage = plan.stages[0]
        runs.ensure(stage.id); runs.acquire(stage.id, "fixture"); runs.reserve(stage.id, "fixture"); runs.release(stage.id, "fixture")
        expect(readRefinementObservation(data.store, task, reference)).toMatchObject({ execution_state: "outcome_unknown",
          stages: [{ state: "DISPATCHING", submission_reserved: true, job_observed: false }, { state: "APPROVED", submission_reserved: false }] })
      }
      if (mode === "cancel") {
        await advanceNativeRefinement(data.store, task, "cancel", context, reference, observer)
        expect(readRefinementObservation(data.store, task, reference)).toMatchObject({ execution_state: "outcome_unknown",
          stages: [{ state: "CANCEL_REQUESTED", submission_reserved: false }, { state: "CANCEL_REQUESTED", submission_reserved: false }] })
        for (const stage of plan.stages) { runs.acquire(stage.id, "fixture"); runs.update(stage.id, "fixture", "CANCELLED"); runs.release(stage.id, "fixture") }
        expect(readRefinementObservation(data.store, task, reference).execution_state).toBe("failed")
      }
      if (mode === "complete" || mode === "tamper") {
        for (const stage of plan.stages) {
          runs.ensure(stage.id); runs.acquire(stage.id, "fixture"); const run = runs.reserve(stage.id, "fixture")
          runs.update(stage.id, "fixture", "VERIFYING", null, run.name)
          runs.complete(stage.id, "fixture", classifiedFixture(stage, 0)); runs.release(stage.id, "fixture")
        }
        expect(readRefinementObservation(data.store, task, reference)).toMatchObject({ execution_state: "completed",
          scientific_assessment: "not_evaluated", remote_artifacts_rehashed: false,
          stages: [{ result_record_verified: true }, { result_record_verified: true }] })
        if (mode === "tamper") {
          data.store.db.query("UPDATE refinement_runs SET manifest='{}' WHERE id=?").run(plan.stages[0].id)
          expect(() => readRefinementObservation(data.store, task, reference)).toThrow()
        }
      }
      expect(workflows.get(plan.id).approval).toBe(grant)
      expect(() => closeRefinementPlanning(data.store, task, scope, "Approved work must remain frozen")).toThrow("confirmation exists")
      expect(data.store.db.query("SELECT * FROM refinement_workflows").all()).toHaveLength(1)
    } finally { data.store.close() }
  })
}

test("host refinement proposal keeps exact identities, quotas and live price arithmetic without granting execution", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const task = data.store.get(data.task.id)
    const fixture = diagnosticFixture(data)
    await diagnoseRefinement(data.store, task.id, new AbortController().signal, async () => fixture.profile,
      async (_task, _profile, nonce) => ({ ...fixture.value, nonce }))
    const subset = { ...proposalFixture(data), policy: refinementSelectionPolicy }
    const rates = { af3: task.plan.steps[1].resource, covdock: task.plan.steps[0].resource, recorded_at: 1000 }
    const result = refinementProposal(data.store, task, subset, rates, 1000)
    expect(result.selection.selected_count).toBe(2) // Never pad this small fixture to eight.
    expect(result.selection.input.candidates).toEqual(data.plan.input.candidates)
    expect(result.stages.map((stage) => [stage.engine, stage.minutes, stage.candidate_count])).toEqual([["af3", 120, 2], ["covdock", 420, 2]])
    expect(result.runtime_basis).toContain("NJOBS=1")
    const priced = refinementProposal(data.store, task, subset,
      { ...rates, covdock: { ...rates.covdock, points_per_hour: 3 } }, 1000)
    expect(priced.stages[1].proposed_points).toBe(21)
    expect(priced.proposed_points).toBe(result.stages[0].proposed_points + 21)
    expect(result.proposed_points).toBe(2)
    expect(result.blockers).toContain("INVALID_LICENSE_SYNTAX")
    expect(result.blockers).toContain("COVDOCK_ENTITLEMENT_NOT_VERIFIED")
    expect(result.startup_checks).toContain("获批")
    expect(result.quality_policy.policy).toEqual(provisionalRefinementQuality)
    expect(result.quality_policy.policy_digest).toBe(digest(provisionalRefinementQuality))
    expect(result.quality_policy.state).toBe("PROPOSED_NOT_APPROVED")
    expect(result.ready_for_approval).toBe(false)
    expect(result.compute_submitted).toBe(false)
    expect(result.resource_approval_granted).toBe(false)
    expect(result.scientific_quality_verified).toBe(false)
    expect(() => refinementProposal(data.store, task, subset, rates, 301001)).toThrow("not recent")
    expect(() => refinementProposal(data.store, task, subset, rates, 999)).toThrow("not recent")
    expect(() => refinementProposal(data.store, task, subset, { ...rates, af3: { ...rates.af3, project: "other" } }, 1000)).toThrow("roles/project")
    expect(() => refinementProposal(data.store, task, { ...subset, policy: { ...subset.policy, max_candidates: 9 } }, rates, 1000)).toThrow()
    const empty = refinementProposal(data.store, task, { ...subset, status: "NO_CANDIDATES_MEET_POLICY", selected_count: 0,
      input: { ...subset.input, candidates: [], identity_sha256: digest([]) } }, rates, 1000)
    expect(empty.blockers).toContain("NO_CANDIDATES_MEET_POLICY")
    expect(empty.selection.input.candidates).toEqual([])
    const nocharge = refinementProposal(data.store, task, subset, { ...rates, af3: { ...rates.af3, points_per_hour: 0 } }, 1000)
    expect(nocharge.proposed_points).toBe(0)
    expect(nocharge.ready_for_approval).toBe(false)
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
    expect(data.store.get(task.id).approval).toBe(task.approval)
  } finally { data.store.close() }
})

test("persisted refinement proposal is invalidated by new diagnostic, tampering or latest failure without old-plan fallback", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const task = data.store.get(data.task.id)
    const subset = { ...proposalFixture(data), policy: refinementSelectionPolicy }
    const value = refinementProposal(data.store, task, subset,
      { af3: task.plan.steps[1].resource, covdock: task.plan.steps[0].resource, recorded_at: 1000 }, 1000)
    expect(value.blockers).toContain("ENVIRONMENT_DIAGNOSTIC_UNAVAILABLE")
    const event = { state: "AVAILABLE", version: refinementProposalVersion, value, digest: digest(value) }
    data.store.event(task.id, "refinement_execution_proposal", event)
    expect(refinementProposalSnapshot(data.store, task)?.proposal).toEqual(value)
    const saved = refinementProposalSnapshot(data.store, task)
    const before = digest(saved)
    const presented = presentRefinement(saved)
    if (!presented || !("interpretation" in presented)) throw new Error("Missing verified presentation")
    expect(presented.proposal).toEqual(value)
    expect(presented.interpretation.rank_definition).toContain("0 不表示零分")
    expect(presented.interpretation.failures).toContain("FAILED / NOT_EVALUATED")
    expect(presented.interpretation.approval).toContain("不另设一次质检审批")
    for (const [carsi, rtm, text] of [[0.867, 0.082, "RTMScore 排名较靠前，CarsiDock 排名较靠后"],
      [0, 0.224, "CarsiDock 排名较靠前，RTMScore 排名较靠后"], [0, 0, "两引擎排名分位相同"]] as const) {
      const row = value.selection.input.candidates[0]
      const view = presentRefinement({ ...saved!, state: "AVAILABLE", proposal: { ...value,
        selection: { ...value.selection, input: { ...value.selection.input, candidates: [{ ...row,
          carsidock_rank_fraction: carsi, rtmscore_rank_fraction: rtm }] } } } })
      expect(view && "interpretation" in view && view.interpretation.candidates[0].relative_rank).toBe(text)
    }
    expect(digest(saved)).toBe(before)
    expect(presentRefinement(null)).toBeNull()
    const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
    try { expect(refinementProposalSnapshot(reopened, reopened.get(task.id))?.proposal).toEqual(value) }
    finally { reopened.close() }
    const altered = { ...value, proposed_points: 0, ready_for_approval: true }
    data.store.event(task.id, "refinement_execution_proposal", { ...event, value: altered, digest: digest(altered) })
    expect(refinementProposalSnapshot(data.store, task)?.state).toBe("UNAVAILABLE")
    expect(presentRefinement(refinementProposalSnapshot(data.store, task))).not.toHaveProperty("interpretation")
    data.store.event(task.id, "refinement_execution_proposal", event)
    const fixture = diagnosticFixture(data)
    await diagnoseRefinement(data.store, task.id, new AbortController().signal, async () => fixture.profile,
      async (_task, _profile, nonce) => ({ ...fixture.value, nonce }))
    expect(refinementProposalSnapshot(data.store, task)?.state).toBe("UNAVAILABLE")
    data.store.event(task.id, "refinement_execution_proposal", { state: "UNAVAILABLE", version: refinementProposalVersion })
    expect(refinementProposalSnapshot(data.store, task)?.proposal).toBeNull()
  } finally { data.store.close() }
})

test("online planning action accepts only the existing task identity, never model-supplied approval or plan fields", () => {
  for (const action of ["plan_refinement", "refinement_status"] as const) {
    const input = { action, task_id: "screen-" + "a".repeat(24) }
    expect(screeningToolSchema.parse(input)).toEqual(input)
    for (const change of [{ selection_policy: refinementSelectionPolicy }, { max_points: null }, { request: "approve" },
      { human_approved: true }, { resource: {} }, { task_id: undefined }]) {
      expect(() => screeningToolSchema.parse({ ...input, ...change })).toThrow()
    }
  }
})

function diagnosticFixture(data: Awaited<ReturnType<typeof setup>>, nonce = "1".repeat(32)) {
  const workflow = workflowFixture(data)
  const af3 = workflow.stages[0].runtime
  const covdock = workflow.stages[1].runtime
  if (af3.name !== "af3" || covdock.name !== "covdock") throw new Error("Wrong fixture runtimes")
  const profile = refinementProfile.parse({ version: "refinement-environment-profile-v1", project: "fixture", target: "EGFR", site: "C797",
    code: { ...data.plan.code, checker_sha256: "a".repeat(64) }, af3, covdock })
  const paths = [af3.python, af3.af3_python, af3.runner, af3.source_data_json, `${af3.model_dir}/af3.bin`, covdock.receptor,
    covdock.license_file, ...["run", "ligprep", "covalent_docking", "utilities/lictest"].map((name) => `${covdock.schrodinger_root}/${name}`)]
  const value = { version: "refinement-environment-diagnostic-v1", task_id: data.task.id, plan_digest: data.task.digest,
    profile_digest: digest(profile), nonce, code: profile.code, assets_stable: true,
    files: paths.map((path) => ({ path, state: "HASHED", bytes: 100, sha256: "a".repeat(64) })),
    af3: { wrapper_imports_passed: true, native_imports_passed: true, model_loaded: false, gpu_verified: false,
      wrapper_check: { reason: "PASSED", returncode: null, seconds_limit: null, failed_modules: [] },
      native_check: { reason: "PASSED", returncode: null, seconds_limit: null, failed_modules: [] },
      cpu_parameters: { state: "PASSED", code: "CPU_PARAMETERS_LOADED", tensor_count: 1, parameter_bytes: 100,
        schema_sha256: "a".repeat(64), error_type: null, phase: "completed",
        loader_path: path.posix.join(path.posix.dirname(af3.runner), "src/alphafold3/model/params.py"),
        loader_sha256: "a".repeat(64), seconds_limit: 60, memory_limit_bytes: 16 * 1024 ** 3, returncode: 0 },
      protein: { state: "PASSED", code: "TARGET_CYS_PRESENT", sequence_length: 330, cysteine_sequence_index: 105,
        unpaired_msa_present: true, paired_msa_present: true, template_count: 4 } },
    covdock: { wrapper_imports_passed: true,
      entitlements: { state: "UNVERIFIED", covdock_available: false, ligprep_available: false,
        source_path: null, source_sha256: null, returncode: null, scope: "vendor_covdock_checkLicense_and_ligprep_availability" },
      wrapper_check: { reason: "PASSED", returncode: null, seconds_limit: null, failed_modules: [] },
      license: { state: "FAILED", code: "INVALID_LICENSE_SYNTAX", feature: "MMLIBS", returncode: 17, covdock_entitlement_verified: false },
      receptor: { state: "PASSED", code: "UNIQUE_CYS_SG_CA", attachment_residue: "A:797" } },
    blockers: ["AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED", "COVDOCK_ENTITLEMENT_NOT_VERIFIED", "INVALID_LICENSE_SYNTAX"],
    ready_to_submit: false, compute_submitted: false, scientific_quality_verified: false, resource_approval_granted: false }
  return { profile, value }
}

test("environment diagnosis binds reader inputs and never upgrades hashes/imports/license prerequisites to readiness", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const task = data.store.get(data.task.id)
    const fixture = diagnosticFixture(data)
    const verify = (value: unknown) => verifyRefinementDiagnostic(value, task, fixture.profile, fixture.value.nonce)
    expect(verify(fixture.value).covdock.license.code).toBe("INVALID_LICENSE_SYNTAX")
    const runtime = fixture.profile.af3
    if (runtime.name !== "af3") throw new Error("Wrong runtime")
    const installed = path.posix.join(path.posix.dirname(path.posix.dirname(runtime.af3_python)), "lib/python3.11/site-packages/alphafold3/model/params.py")
    expect(verify({ ...fixture.value, af3: { ...fixture.value.af3,
      cpu_parameters: { ...fixture.value.af3.cpu_parameters, loader_path: installed } } }).af3.cpu_parameters.loader_path).toBe(installed)
    for (const change of [{ nonce: "0".repeat(32) }, { plan_digest: "0".repeat(64) }, { profile_digest: "0".repeat(64) },
      { ready_to_submit: true }, { compute_submitted: true }, { resource_approval_granted: true },
      { blockers: [] }, { files: fixture.value.files.slice(1) },
      { files: fixture.value.files.map((file) => ({ ...file, path: "/inspire/another" })) },
      { af3: { ...fixture.value.af3, model_loaded: true } },
      { af3: { ...fixture.value.af3, cpu_parameters: { ...fixture.value.af3.cpu_parameters, tensor_count: null } } },
      { af3: { ...fixture.value.af3, cpu_parameters: { ...fixture.value.af3.cpu_parameters, returncode: 1 } } },
      { af3: { ...fixture.value.af3, cpu_parameters: { ...fixture.value.af3.cpu_parameters, loader_path: "/inspire/wrong" } } },
      { af3: { ...fixture.value.af3, protein: { ...fixture.value.af3.protein, sequence_length: 10 } } },
      { covdock: { ...fixture.value.covdock, license: { ...fixture.value.covdock.license, state: "UNVERIFIED" } } },
      { covdock: { ...fixture.value.covdock, license: { ...fixture.value.covdock.license, raw: "must not publish" } } },
      { covdock: { ...fixture.value.covdock, receptor: { ...fixture.value.covdock.receptor, attachment_residue: "A:773" } } },
    ]) expect(() => verify({ ...fixture.value, ...change })).toThrow()
    const args = refinementDiagnosticArgs(task, fixture.profile, fixture.value.nonce)
    expect(args.slice(0, 7)).toEqual(["notebook", "exec", "fixture", "--workspace", "CPU资源空间", "--timeout", "180"])
    expect(args.at(-1)).toContain("def license_probe")
    expect(args.at(-1)).toContain("def inspect(runtime, science)")
    expect(args.at(-1)).not.toContain("--stdin")
    expect(() => refinementDiagnosticArgs(task, { ...fixture.profile, project: "another" }, fixture.value.nonce)).toThrow("another project")
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

test("latest persisted environment diagnosis survives reopen and failed new reads never fall back to older evidence", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const fixture = diagnosticFixture(data)
    const core = data.store.get(data.task.id).approval
    const signal = new AbortController().signal
    const first = await diagnoseRefinement(data.store, data.task.id, signal, async () => fixture.profile,
      async (_task, _profile, nonce) => ({ ...fixture.value, nonce }))
    expect(first?.state).toBe("AVAILABLE")
    expect(first?.ready_to_submit).toBe(false)
    expect(first?.result?.blockers).toContain("INVALID_LICENSE_SYNTAX")
    const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
    try { expect(refinementDiagnosticSnapshot(reopened, reopened.get(data.task.id))).toEqual(first) }
    finally { reopened.close() }
    const second = await diagnoseRefinement(data.store, data.task.id, signal, async () => fixture.profile,
      async () => { throw new Error("PRIVATE_TEST_ERROR_MUST_NOT_PUBLISH") })
    expect(second?.state).toBe("UNAVAILABLE")
    expect(second?.result).toBeNull()
    expect(JSON.stringify(data.store.events(data.task.id))).not.toContain("PRIVATE_TEST_ERROR_MUST_NOT_PUBLISH")
    expect(data.store.get(data.task.id).approval).toBe(core)
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

test("environment diagnosis abort/profile change cannot persist a usable witness or make a remote call after abort", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const fixture = diagnosticFixture(data)
    const calls: string[] = []
    const controller = new AbortController()
    controller.abort()
    const read = async (_task: unknown, _profile: unknown, nonce: string) => { calls.push("read"); return { ...fixture.value, nonce } }
    expect((await diagnoseRefinement(data.store, data.task.id, controller.signal, async () => fixture.profile, read))?.state).toBe("UNAVAILABLE")
    expect(calls).toEqual([])
    const values = [fixture.profile, { ...fixture.profile, project: "changed" }]
    const result = await diagnoseRefinement(data.store, data.task.id, new AbortController().signal,
      async () => values.shift()!, read)
    expect(result?.state).toBe("UNAVAILABLE")
    expect(calls).toEqual(["read"])
  } finally { data.store.close() }
})

function workflowFixture(data: Awaited<ReturnType<typeof setup>>) {
  const af3 = refinementPlan.parse({ ...data.plan, result_contract: "screening-refinement-outcome-v2",
    finish_before: data.plan.approve_before + 60 * 60000 })
  const runtime = { name: "covdock" as const, python: "/inspire/python", receptor: "/inspire/receptor.mae",
    schrodinger_root: "/inspire/schrodinger", license_file: "/inspire/license.lic", runtime_lib_dir: "/inspire/libs", attachment_residue: "A:797" as const }
  const covdock = refinementPlan.parse({ ...af3, id: "refine-" + "b".repeat(24), runtime,
    engine: { name: "covdock", mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.95 },
    resource: data.task.plan.steps[0].resource, max_points: 0,
    assets: { ...af3.assets, ...Object.fromEntries(refinementPaths(runtime, data.task.plan.runtime.root, af3.code.release_id).files.map((file) => [file, "a".repeat(64)])) } })
  return refinementWorkflow.parse({ version: "screening-refinement-workflow-v1", id: "refineflow-" + "c".repeat(24),
    stages: [af3, covdock], execution: "af3_then_covdock_same_subset", failure_policy: "stop_on_execution_failure",
    quality_policy: "retain_rejections_and_continue", max_points: af3.max_points + covdock.max_points })
}

test("workflow freezes both engine scopes, same subset, shared assets, budget and sufficient sequential time", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const plan = workflowFixture(data)
    const [af3, covdock] = plan.stages
    const stages = (change: object) => [af3, { ...covdock, ...change }]
    for (const change of [
      { max_points: 0 }, { execution: "parallel" }, { failure_policy: "retry" }, { quality_policy: "drop_rejections" },
      { stages: [covdock, af3] }, { stages: [af3, af3] }, { stages: [af3] },
      { stages: stages({ id: af3.id }) }, { stages: stages({ session: "another" }) },
      { stages: stages({ selection_manifest_digest: "0".repeat(64) }) },
      { stages: stages({ code: { ...covdock.code, release_id: "0".repeat(64) } }) },
      { stages: stages({ input: { ...covdock.input, sha256: "0".repeat(64) } }) },
      { stages: stages({ result_contract: undefined }) },
      { stages: stages({ assets: { ...covdock.assets, "/inspire/python": "0".repeat(64) } }) },
      { stages: plan.stages.map((stage) => ({ ...stage, finish_before: stage.approve_before + 60000 })) },
    ]) expect(() => refinementWorkflow.parse({ ...plan, ...change })).toThrow()
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

for (const decision of ["once", "once_qc", "reject", "abort", "cancel", "expire", "tamper", "changed_file", "parent_changed"] as const) {
  test(`single native workflow approval binds both engines durably without independent receipts: ${decision}`, async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const data = await setup(tmp.path)
      const initial = workflowFixture(data)
      const plan = refinementWorkflow.parse({ ...initial, stages: initial.stages.map((stage) => ({ ...stage,
        ...(decision === "once_qc" ? { quality_policy: defaultRefinementQuality,
          ...(stage.engine.name === "af3" ? { geometry_contract: "af3-geometry-evidence-v1" } : {}) } : {}),
        ...(decision === "expire" ? { approve_before: Date.now() + 300 } : {}) })) })
      const workflows = new RefinementWorkflows(data.refinements)
      const core = data.store.get(data.task.id).approval
      const controller = new AbortController()
      const context = { sessionID: plan.stages[0].session, directory: tmp.path, abort: controller.signal,
        metadata: () => {}, ask: (input: Parameters<typeof PermissionNext.ask>[0]) => PermissionNext.ask(input) }
      const requested = Promise.withResolvers<PermissionNext.Request>()
      const cards: PermissionNext.Request[] = []
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID !== context.sessionID) return
        cards.push(event.properties)
        requested.resolve(event.properties)
      })
      const checks: string[] = []
      const pending = proposeRefinementWorkflow(workflows, plan, { ...context,
        ask: (input) => PermissionNext.ask({ ...input, sessionID: context.sessionID, signal: controller.signal,
          ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }),
      }, async (stage) => {
        checks.push(stage.engine.name)
        if (await data.checksum() !== stage.input.sha256) throw new Error("Actual subset bytes changed during workflow approval")
      }).then((task) => ({ task, error: undefined }), (error: unknown) => ({ task: undefined, error }))
      try {
        const card = await requested.promise
        expect(card.patterns).toEqual([digest(plan)])
        expect(card.metadata.compute.stages.map((stage: { phase: string }) => stage.phase)).toEqual(["af3", "covdock"])
        expect(card.metadata.compute.selected_candidates).toEqual(plan.stages[0].input.candidates)
        expect(card.metadata.compute.maximum_compute_points).toBe(plan.max_points)
        expect(card.metadata.compute.minutes).toBe(20)
        expect(card.metadata.compute.execution).toBe("af3_then_covdock_same_subset")
        expect(card.metadata.compute.scientific_quality_policy).toEqual(decision === "once_qc" ? defaultRefinementQuality : null)
        expect(() => new RefinementRuns(data.refinements).ensure(plan.stages[0].id)).toThrow()
        expect(() => data.refinements.approve(plan.stages[0].id, digest(plan.stages[0]), { ...context, ask: async () => {} })).toThrow("single exact-workflow")
        if (decision === "abort") controller.abort()
        if (decision === "cancel") workflows.endApproval(plan.id, digest(plan), context)
        if (decision === "expire") await Bun.sleep(Math.max(0, plan.stages[0].approve_before - Date.now() + 10))
        if (decision === "tamper") data.store.db.query("UPDATE refinements SET plan=? WHERE id=?")
          .run(JSON.stringify({ ...plan.stages[1], minutes: 9 }), plan.stages[1].id)
        if (decision === "changed_file") await Bun.write(data.file, "mutated actual local subset")
        if (decision === "parent_changed") data.store.state(data.task.id, "NEEDS_REVIEW")
        if (decision !== "abort") await PermissionNext.reply({ requestID: card.id, reply: decision === "reject" ? "reject" : "once" })
        const result = await pending
        expect(cards).toHaveLength(1)
        expect(data.store.get(data.task.id).approval).toBe(core)
        expect(data.store.db.query("SELECT * FROM refinement_runs").all()).toEqual([])
        if (decision === "once" || decision === "once_qc") {
          expect(result.error).toBeUndefined()
          expect(result.task?.state).toBe("APPROVED")
          expect(checks).toEqual(["af3", "covdock", "af3", "covdock"])
          const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
          try {
            const approvals = new Refinements(reopened)
            const runs = new RefinementRuns(approvals)
            for (const stage of plan.stages) {
              expect(approvals.get(stage.id).approval).toBeNull()
              expect(approvals.get(stage.id).state).toBe("WORKFLOW_APPROVED")
              expect(approvals.get(stage.id).plan.quality_policy).toEqual(decision === "once_qc" ? defaultRefinementQuality : undefined)
              expect(runs.ensure(stage.id).state).toBe("WAITING")
            }
            expect(runs.ready(plan.stages[0].id)).toBe(true)
            expect(runs.ready(plan.stages[1].id)).toBe(false)
          } finally { reopened.close() }
        } else {
          expect(result.error).toBeDefined()
          expect(data.store.db.query("SELECT approval FROM refinement_workflows WHERE id=?").get(plan.id)).toEqual({ approval: null })
          if (decision !== "tamper") {
            expect(workflows.get(plan.id).state).toBe("CANCELLED")
            expect(plan.stages.every((stage) => data.refinements.get(stage.id).state === "CANCELLED")).toBe(true)
          }
          if (decision === "abort") {
            await PermissionNext.reply({ requestID: card.id, reply: "once" })
            expect(workflows.get(plan.id).state).toBe("CANCELLED")
          }
        }
      } finally { unsubscribe(); data.store.close() }
    } })
  })
}

test("workflow refuses license/preflight failure before any permission request or partial persisted grant", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const workflows = new RefinementWorkflows(data.refinements)
    const plan = workflowFixture(data)
    const calls: string[] = []
    await expect(proposeRefinementWorkflow(workflows, plan, { sessionID: plan.stages[0].session, directory: tmp.path,
      abort: new AbortController().signal, metadata: () => { calls.push("metadata") }, ask: async () => { calls.push("ask") },
    }, async (stage) => { calls.push(stage.engine.name); if (stage.engine.name === "covdock") throw new Error("Fixture license preflight refused") }))
      .rejects.toThrow("license preflight refused")
    expect(calls).toEqual(["af3", "covdock"])
    expect(data.store.db.query("SELECT * FROM refinement_workflows").all()).toEqual([])
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

test("workflow execution enforces AF3 result validation, persists rejection outcomes, and checks grant provenance on reserve", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const workflows = new RefinementWorkflows(data.refinements)
    const plan = workflowFixture(data)
    const [af3, covdock] = plan.stages
    workflows.create(plan)
    workflows.approve(plan.id, digest(plan), { sessionID: af3.session, directory: tmp.path,
      abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
    const runs = new RefinementRuns(data.refinements)
    runs.ensure(af3.id)
    runs.ensure(covdock.id)
    runs.acquire(covdock.id, "owner")
    expect(() => runs.reserve(covdock.id, "owner")).toThrow("wait for verified AF3")
    expect(runs.get(covdock.id).submitted).toBeNull()
    runs.acquire(af3.id, "owner")
    const first = runs.reserve(af3.id, "owner")
    runs.update(af3.id, "owner", "VERIFYING", null, first.name)
    expect(runs.ready(covdock.id)).toBe(false)
    // Local contract fixtures only, not an AF3 run or scientific acceptance.
    const result = classifiedFixture(af3, 0)
    runs.complete(af3.id, "owner", result)
    expect(runs.ready(covdock.id)).toBe(true)
    data.store.db.query("UPDATE refinement_runs SET manifest=? WHERE id=?").run(JSON.stringify({ ...result, input_sha256: "0".repeat(64) }), af3.id)
    expect(() => runs.reserve(covdock.id, "owner")).toThrow()
    data.store.db.query("UPDATE refinement_runs SET manifest=? WHERE id=?").run(JSON.stringify(result), af3.id)
    const receipt = workflows.get(plan.id).approval!
    data.store.db.query("UPDATE refinement_workflows SET approval=? WHERE id=?").run(JSON.stringify({ ...JSON.parse(receipt), scope: "core" }), plan.id)
    expect(() => runs.reserve(covdock.id, "owner")).toThrow()
    data.store.db.query("UPDATE refinement_workflows SET approval=? WHERE id=?").run(receipt, plan.id)
    const second = runs.reserve(covdock.id, "owner")
    expect(second.state).toBe("DISPATCHING")
    expect(() => runs.reserve(covdock.id, "owner")).toThrow("repeat submission")
    expect(plan.stages.every((stage) => data.refinements.get(stage.id).approval === null)).toBe(true)
    expect(data.store.events(data.task.id).filter((event) => (event as { kind: string }).kind === "refinement_workflow_approved")).toHaveLength(1)
  } finally { data.store.close() }
})

for (const mode of ["success", "failed", "wrong_identity"] as const) {
  test(`workflow resumes the same real local subprocesses and stops downstream on AF3 failure: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path)
    const plan = workflowFixture(data)
    const workflows = new RefinementWorkflows(data.refinements)
    workflows.create(plan)
    workflows.approve(plan.id, digest(plan), { sessionID: plan.stages[0].session, directory: tmp.path,
      abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
    // Actual child processes/files exercise the controller; contents are visibly
    // non-scientific fixtures and cannot count as remote engine acceptance.
    const af3 = processTransport(path.join(tmp.path, "af3-fixture"), mode, 40, true, true)
    const covdock = processTransport(path.join(tmp.path, "covdock-fixture"), "success", 40, true, true)
    const select = (plan: RefinementPlan) => plan.engine.name === "af3" ? af3.transport : covdock.transport
    const transport: RefinementTransport = {
      prepare: (plan, run) => select(plan).prepare(plan, run),
      preflight: (plan, run) => select(plan).preflight(plan, run),
      submit: (plan, run) => select(plan).submit(plan, run),
      observe: (plan, run) => select(plan).observe(plan, run),
      stop: (plan, run) => select(plan).stop(plan, run),
      verify: (plan, run) => select(plan).verify(plan, run),
    }
    const runs = new RefinementRuns(data.refinements)
    await new RefinementRunner(runs, transport).tickWorkflow(plan.id)
    expect(runs.get(plan.stages[0].id).state).toBe("DISPATCHING")
    expect(runs.get(plan.stages[1].id).state).toBe("WAITING")
    expect(covdock.count()).toBe(0)
    data.store.close()
    await af3.finished()
    const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
    const resumed = new RefinementRuns(new Refinements(reopened))
    const runner = new RefinementRunner(resumed, transport)
    try {
      await runner.tickWorkflow(plan.id)
      await covdock.finished()
      await runner.tickWorkflow(plan.id)
      await runner.tickWorkflow(plan.id)
      expect(af3.count()).toBe(1)
      expect(covdock.count()).toBe(mode === "success" ? 1 : 0)
      expect(resumed.get(plan.stages[0].id).state).toBe(mode === "success" ? "SUCCEEDED" : mode === "failed" ? "FAILED" : "NEEDS_REVIEW")
      expect(resumed.get(plan.stages[1].id).state).toBe(mode === "success" ? "SUCCEEDED" : mode === "failed" ? "CANCELLED" : "WAITING")
      if (mode === "success") {
        for (const stage of plan.stages) expect(JSON.parse(resumed.get(stage.id).manifest!).quality_counts).toEqual({ passed: 0, rejected: 2, not_evaluated: 0 })
      }
      if (mode !== "success") expect(resumed.get(plan.stages[1].id).submitted).toBeNull()
      expect(resumed.list()).toHaveLength(2)
    } finally { await af3.cleanup(); await covdock.cleanup(); reopened.close() }
  })
}

for (const engine of ["af3", "covdock"]) {
  test(`refinement ${engine} distinguishes create acknowledgement, queue and observed execution`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await cliFixture(tmp.path, engine)
    try {
      const runner = new RefinementRunner(data.runs, data.transport)
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("SUBMITTED")
      await data.configure({ status: "QUEUEING" })
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("QUEUED")
      await data.configure({ status: "RUNNING" })
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("RUNNING")
      await runner.tick(data.plan.id)
      expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(1)
    } finally { data.store.close() }
  })
}

test("verified workflow entry cannot replace source evidence with a permissive runtime callback", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const plan = workflowFixture(data)
    const workflows = new RefinementWorkflows(data.refinements)
    const calls: string[] = []
    await expect(proposeVerifiedRefinementWorkflow(workflows, plan, { sessionID: plan.stages[0].session,
      directory: tmp.path, abort: new AbortController().signal, metadata: () => { calls.push("metadata") },
      ask: async () => { calls.push("ask") },
    }, new Inspire(data.task.plan.connection), async () => { calls.push("preflight") })).rejects.toThrow("source report not currently verified")
    expect(calls).toEqual([])
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

function proposalFixture(data: Awaited<ReturnType<typeof setup>>): ReturnType<typeof verifySelectionProposal> {
  return { selection_verified: true, selection_id: data.plan.selection_id, source_task_id: data.task.id,
    source_plan_digest: data.task.digest,
    source_manifest_digest: digest(JSON.parse(data.store.stages(data.task.id)[3].manifest!)),
    selection_manifest_digest: data.plan.selection_manifest_digest, status: "SUBSET_PROPOSED", input_count: data.task.plan.shortlist,
    eligible_count: data.task.plan.shortlist, excluded_count: 0, selected_count: data.plan.input.candidates.length,
    effective_cap: Math.min(2, Math.floor(data.task.plan.shortlist * 0.25)),
    artifact_directory: data.plan.input.path.slice(0, -"/candidates.parquet".length), input: data.plan.input,
    policy: { max_candidates: 2, max_fraction: 0.25, disagreement_threshold: 0.5, top_fraction: 0.2,
      uncertainty_count: 1, consensus_count: data.plan.input.candidates.length - 1, diversity_count: 0 },
    calibrated_uncertainty: false, gpu_submitted: false, refinement_approval_required: true,
    limitations: ["Local fixture; rank disagreement is not calibrated uncertainty"] }
}

test("platform selection proposal checks full identity, policy, channel reasons, caps and empty outcomes", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const task = data.store.get(data.task.id)
    const stages = data.store.stages(task.id)
    const proof = proposalFixture(data)
    const check = (value: unknown) => verifySelectionProposal(value, task, stages, proof.policy)
    expect(check(proof).input.candidates).toEqual(data.plan.input.candidates)
    for (const change of [{ selection_verified: false }, { source_task_id: "other" },
      { source_plan_digest: "0".repeat(64) }, { source_manifest_digest: "0".repeat(64) },
      { policy: { ...proof.policy, disagreement_threshold: 0.1 } }, { selected_count: 1 }, { effective_cap: 1 },
      { eligible_count: 9 }, { input_count: 9 }, { excluded_count: 1 }, { status: "NO_CANDIDATES_MEET_POLICY" },
      { artifact_directory: "/inspire/old-pilot" }, { gpu_submitted: true }, { calibrated_uncertainty: true },
      { refinement_approval_required: false }, { standardized_smiles: ["must not leak"] },
      { input: { ...proof.input, path: "/inspire/old-pilot/candidates.parquet" } },
      { input: { ...proof.input, identity_sha256: "0".repeat(64) } },
      { input: { ...proof.input, candidates: [proof.input.candidates[0], proof.input.candidates[0]] } },
      { input: { ...proof.input, eligible_count: 7 } },
      { input: { ...proof.input, candidates: proof.input.candidates.map((row) => ({ ...row, rank_disagreement: 0.3 })) } },
      { input: { ...proof.input, candidates: proof.input.candidates.map((row) => ({ ...row, selection_channel: "diversity_sentinel" })) } },
      { input: { ...proof.input, candidates: proof.input.candidates.map((row) => ({ ...row, selection_channel: "consensus_sentinel" })) } },
      { input: { ...proof.input, candidates: [{ ...proof.input.candidates[0], carsidock_rank_fraction: 0.1,
        rtmscore_rank_fraction: 0.2, rank_disagreement: 0.1 }, proof.input.candidates[1]] } },
      { input: { ...proof.input, candidates: [proof.input.candidates[0], { ...proof.input.candidates[1],
        rtmscore_rank_fraction: 0.3, rank_disagreement: 0.2 }] } },
    ]) expect(() => check({ ...proof, ...change })).toThrow()
    for (const eligible of [0, 3, 8]) {
      const empty = { ...proof, status: "NO_CANDIDATES_MEET_POLICY", selected_count: 0, eligible_count: eligible,
        excluded_count: 8 - eligible, effective_cap: Math.min(2, Math.floor(eligible * 0.25)),
        input: { ...proof.input, eligible_count: eligible, candidates: [], identity_sha256: digest([]) } }
      expect(check(empty).selected_count).toBe(0)
      expect(() => check({ ...empty, status: "SUBSET_PROPOSED" })).toThrow()
    }
    expect(() => verifySelectionProposal(proof, { ...task, approval: null }, stages, proof.policy)).toThrow()
    expect(() => verifySelectionProposal(proof, task, [...stages].reverse(), proof.policy)).toThrow()
    expect(data.store.events(task.id).some((row) => (row as { kind: string }).kind === "refinement_subset_proposed")).toBe(false)
  } finally { data.store.close() }
})

for (const mode of ["valid", "empty", "changed", "unavailable", "bad_source", "aborted", "read_bound"] as const) {
  test(`platform selection uses bound CLI, full core readback and exact second witness before persistence: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const cli = path.join(tmp.path, "selection-cli.ts")
    await Bun.write(cli, `#!${process.execPath}\n` + await Bun.file(path.join(import.meta.dir, "selection-cli.fixture.ts")).text())
    await chmod(cli, 0o700)
    const data = await setup(tmp.path, cli)
    try {
      const artifact = "Local file-chain fixture only"
      await Bun.write(cli + ".artifact", artifact)
      const stages = data.store.stages(data.task.id)
      const manifests: Record<string, unknown>[] = []
      for (const stage of stages) {
        const manifest = { ...JSON.parse(stage.manifest!), parent_digest: manifests.length ? digest(manifests.at(-1)) : null,
          artifacts: { "fixture.txt": new Bun.CryptoHasher("sha256").update(artifact).digest("hex") } }
        manifests.push(manifest)
        data.store.stage(data.task.id, stage.ordinal, { manifest: JSON.stringify(manifest) })
        await Bun.write(cli + "." + stage.phase + ".json", JSON.stringify(manifest))
      }
      const proof = proposalFixture(data)
      const result: ReturnType<typeof verifySelectionProposal> = mode === "empty" ? { ...proof, status: "NO_CANDIDATES_MEET_POLICY", selected_count: 0,
        input: { ...proof.input, candidates: [], identity_sha256: digest([]) } } : proof
      await Bun.write(cli + ".json", JSON.stringify({ task: data.task.id, digest: data.task.digest, proposal: result,
        ...(mode === "changed" ? { readback: { ...result, selection_manifest_digest: "0".repeat(64) } } : {}),
        readback_error: mode === "unavailable" }))
      if (mode === "bad_source") await Bun.write(cli + ".artifact", "corrupted")
      const controller = new AbortController()
      if (mode === "aborted") controller.abort()
      const task = data.store.get(data.task.id)
      const pending = mode === "read_bound" ? readSelectionProposal(data.store, task, {
        selection_id: result.selection_id, selection_manifest_digest: result.selection_manifest_digest,
        policy: result.policy, reason: "Read only the explicitly reviewed subset" }, new Inspire(task.plan.connection), controller.signal)
        : selectRefinementProposal(data.store, task, result.policy, new Inspire(task.plan.connection), controller.signal)
      if (mode === "valid" || mode === "empty" || mode === "read_bound") expect(await pending).toEqual(result)
      if (mode !== "valid" && mode !== "empty" && mode !== "read_bound") await expect(pending).rejects.toThrow()
      const events = data.store.events(task.id).filter((row) => (row as { kind: string }).kind === "refinement_subset_proposed")
      expect(events).toHaveLength(mode === "valid" || mode === "empty" ? 1 : 0)
      const calls: string[][] = await Bun.file(cli + ".calls").exists()
        ? (await Bun.file(cli + ".calls").text()).trim().split("\n").map((line) => JSON.parse(line)) : []
      expect(calls).toHaveLength(mode === "aborted" ? 0 : mode === "bad_source" ? 1 : mode === "read_bound" ? 5 : 6)
      expect(calls.every((args) => args[4] === "notebook" && args[5] === "exec")).toBe(true)
      expect(data.store.get(task.id).approval).toBe(task.approval)
      expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
    } finally { data.store.close() }
  })
}

// Non-scientific local evidence fixtures; never contact Inspire or invoke engines.
async function setup(directory: string, cli = "/never-invoked", engine = "af3", classified = false, session = "session_refinement", bound = false, research = false) {
  const store = new Store(path.join(directory, "refinement.sqlite"))
  const cpu = { kind: "hpc", workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
    image: "fixture", image_type: "SOURCE_OFFICIAL", priority: 4, points_per_hour: 0 }
  const gpu = { ...cpu, kind: "job", workspace: "分布式训练空间", quota: "1,10,200", points_per_hour: 1 }
  const example = researchFixture(directory)
  example.environment.connection.cli = cli
  const compiled = research ? prepareWorkflowScreening(store, example.intent, { directory, sessionID: session }, example.environment) : null
  const core = compiled?.task.plan ?? planSchema.parse({ version: "screening-executor-v1", id: "screen-" + "a".repeat(24),
    request: "fixture core", directory, session, target: "EGFR", site: "C797",
    ...(bound ? { origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant",
      part_ids: ["fixture-text"], request_sha256: requestHash("fixture core") } } : {}),
    library: "CovInDB/Virtual_Screening_Library.csv", scope: "bounded", max_records: 100, shortlist: 8, seed: 1,
    connection: { cli, account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    assets: Object.fromEntries(["a", "b", "c", "d", "e"].map((item) => ["/inspire/" + item, "a".repeat(64)])),
    runtime: { project: "/inspire/project", root: "/inspire/root", worker: "/inspire/worker", activation: "/inspire/activation" },
    steps: ["prepare", "carsidock", "rtmscore", "report"].map((phase) => ({ phase, minutes: 1, max_candidates: 8,
      resource: ["carsidock", "rtmscore"].includes(phase) ? gpu : cpu })), max_points: 1,
    gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 },
    created: Date.now(), approve_before: Date.now() + 60000, finish_before: Date.now() + 120000 })
  const reference = compiled?.reference ?? { schema: "library-workflow-execution-reference-v1" as const, workflow_id: "library-flow-" + "a".repeat(24),
    revision: 7, operation_id: "b".repeat(32), intent_sha256: "c".repeat(64), plan_sha256: digest(core) }
  const task = compiled?.task ?? (bound ? prepareWorkflowExecution(store, reference, core, { directory, sessionID: session }).task : store.create(core))
  store.approve(task.id, task.digest, core.session, directory)
  const manifests: Record<string, unknown>[] = []
  for (const stage of store.stages(task.id)) {
    const manifest = { task_id: task.id, phase: stage.phase, plan_digest: task.digest, status: "SUCCEEDED",
      parent_digest: manifests.length ? digest(manifests.at(-1)) : null, input_count: core.shortlist, success_count: core.shortlist, failed_count: 0,
      artifacts: { "fixture.txt": "b".repeat(64) }, identity_digest: "c".repeat(64), gates: { fixture: true }, runtime_seconds: 1 }
    manifests.push(manifest)
    store.stage(task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest) })
  }
  store.state(task.id, "SUCCEEDED")
  const request = "fixture independent refinement"
  const candidates = [
    { input_sdf_index: 1, compound_id: "fixture-one", selection_channel: "rank_disagreement_review", carsidock_rank_fraction: 0, rtmscore_rank_fraction: 1, rank_disagreement: 1 },
    { input_sdf_index: 3, compound_id: "fixture-three", selection_channel: "consensus_sentinel", carsidock_rank_fraction: 0.1, rtmscore_rank_fraction: 0.1, rank_disagreement: 0 },
  ].slice(0, Math.min(2, Math.floor(core.shortlist * 0.25)))
  const file = path.join(directory, "subset-fixture.json")
  await Bun.write(file, JSON.stringify(candidates))
  const checksum = async () => new Bun.CryptoHasher("sha256").update(await Bun.file(file).arrayBuffer()).digest("hex")
  const runtime = { name: "af3" as const, python: "/inspire/python", runner: "/inspire/af3/run.py",
    af3_python: "/inspire/af3/python", source_data_json: "/inspire/protein.json", model_dir: "/inspire/models",
    database_dir: "/inspire/databases", protein_chain: "A" as const, ligand_chain: "L" as const,
    cysteine_sequence_index: 105, source_residue_number: 797 as const }
  const plan = refinementPlan.parse({ version: "screening-refinement-v1", id: "refine-" + "d".repeat(24),
    ...(classified ? { result_contract: "screening-refinement-outcome-v2" } : {}),
    request, origin: { user_message_id: "user", assistant_message_id: "assistant", part_ids: ["part"], request_sha256: requestHash(request) },
    directory, session: core.session, parent_id: task.id, parent_digest: task.digest, report_digest: digest(manifests[3]),
    selection_id: "e".repeat(64), selection_manifest_digest: "f".repeat(64),
    code: { release_id: "7".repeat(64), manifest_sha256: "8".repeat(64) },
    runtime,
    input: { path: `${core.runtime.root}/tasks/${task.id}/selections/${"e".repeat(64)}/candidates.parquet`,
      sha256: await checksum(), identity_sha256: digest(candidates.map((row) => [row.input_sdf_index, row.compound_id])),
      candidates, eligible_count: core.shortlist, calibrated_uncertainty: false },
    engine: { name: "af3", num_recycles: 3, num_seeds: 1, num_diffusion_samples: 1, run_data_pipeline: false,
      cpu_per_search: 2, random_seed: 20260725, min_coverage: 0.95 },
    assets: { ...core.assets, ...Object.fromEntries(refinementPaths(runtime, core.runtime.root, "7".repeat(64)).files.map((file) => [file, "a".repeat(64)])) },
    resource: gpu, minutes: 10, max_points: 0.2,
    created: Date.now(), approve_before: Date.now() + 60000, finish_before: Date.now() + 120000 })
  const covdock = { name: "covdock" as const, python: "/inspire/python", receptor: "/inspire/receptor.pdb",
    schrodinger_root: "/inspire/schrodinger", license_file: "/inspire/license.lic", runtime_lib_dir: "/inspire/libs", attachment_residue: "A:773" as const }
  const selected = engine === "af3" ? plan : refinementPlan.parse({ ...plan, runtime: covdock,
    engine: { name: "covdock", mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.95 },
    resource: cpu, assets: { ...plan.assets, ...Object.fromEntries(refinementPaths(covdock, core.runtime.root, plan.code.release_id).files.map((file) => [file, "a".repeat(64)])) } })
  return { store, plan: selected, file, checksum, refinements: new Refinements(store), task, reference: bound ? reference : undefined,
    research: research ? example : null }
}

function resultFixture(plan: RefinementPlan) {
  return { version: "screening-refinement-outcome-v1", task_id: plan.id, plan_digest: digest(plan), engine: plan.engine.name,
    selection_id: plan.selection_id, selection_manifest_digest: plan.selection_manifest_digest,
    input_sha256: plan.input.sha256, input_identity_sha256: plan.input.identity_sha256, code: plan.code,
    input_count: plan.input.candidates.length, success_count: plan.input.candidates.length, failed_count: 0,
    candidates: plan.input.candidates.map((row, index) => ({ input_sdf_index: row.input_sdf_index,
      compound_id: row.compound_id, status: "SUCCESS", score: index + 1, structure: `fixture-${index}.cif`,
      structure_index: plan.engine.name === "covdock" ? index : null, failure_reason: null })),
    score_direction: "lower_is_better", artifacts: {} as Record<string, string>,
    gates: { input_integrity: true, output_integrity: true, coverage: true, scientific_quality: true } }
}

// Contract fixture only: these verdicts are not measured scientific evidence.
function classifiedFixture(plan: RefinementPlan, passed = 0) {
  const result = resultFixture(plan)
  return { ...result, version: "screening-refinement-outcome-v2",
    artifacts: Object.fromEntries(result.candidates.map((row, index) => [row.structure, String(index + 1).repeat(64)])),
    quality_counts: { passed, rejected: result.input_count - passed, not_evaluated: 0 },
    candidates: result.candidates.map((row, index) => ({ ...row, quality: {
      status: index < passed ? "PASSED" : "REJECTED",
      reason: index < passed ? null : "Fixture quality rule rejected this pose; no experimental conclusion",
      evidence: [row.structure],
    } })),
  }
}

for (const engine of ["af3", "covdock"]) {
  test(`classified ${engine} outcomes preserve a zero-pass scientific result without inventing execution failures`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path, "/never-invoked", engine, true)
    try {
      for (const passed of [0, 1, 2]) {
        const result = classifiedFixture(data.plan, passed)
        const checked = verifyRefinementResult(result, data.plan)
        expect(checked.version).toBe("screening-refinement-outcome-v2")
        expect(checked.success_count).toBe(2)
        expect(checked.failed_count).toBe(0)
        expect(checked.candidates.map((row) => [row.score, row.structure])).toEqual(result.candidates.map((row) => [row.score, row.structure]))
        if (checked.version === "screening-refinement-outcome-v2") expect(checked.quality_counts).toEqual(result.quality_counts)
      }
      const result = classifiedFixture(data.plan)
      for (const change of [
        { quality_counts: { passed: 1, rejected: 2, not_evaluated: 0 } },
        { quality_counts: { passed: 0, rejected: 1, not_evaluated: 1 } },
        { success_count: 0, failed_count: 2 },
        { candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, reason: " " } })) },
        { candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, evidence: [] } })) },
        { candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, evidence: ["unknown.json"] } })) },
        { candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, evidence: [row.structure, row.structure] } })) },
        { candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, status: "NOT_EVALUATED" } })) },
        { gates: { ...result.gates, scientific_quality: false } },
      ]) expect(() => verifyRefinementResult({ ...result, ...change }, data.plan)).toThrow()
      // A real engine failure still reduces coverage. It cannot be disguised as
      // a legitimate negative scientific result to bypass the approved minimum.
      expect(() => verifyRefinementResult({ ...result, success_count: 1, failed_count: 1,
        quality_counts: { passed: 0, rejected: 1, not_evaluated: 1 },
        candidates: [result.candidates[0], { ...result.candidates[1], status: "FAILED", score: null,
          structure: null, structure_index: null, failure_reason: "Fixture engine failed",
          quality: { status: "NOT_EVALUATED", reason: null, evidence: [] } }],
      }, data.plan)).toThrow("coverage")
    } finally { data.store.close() }
  })

  test(`classified ${engine} zero-pass results persist distinctly across SQLite reopen without submitting compute`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path, "/never-invoked", engine, true)
    const result = classifiedFixture(data.plan)
    try {
      const runs = approveFixture(data)
      runs.ensure(data.plan.id)
      runs.acquire(data.plan.id, "fixture-owner")
      const reserved = runs.reserve(data.plan.id, "fixture-owner")
      runs.update(data.plan.id, "fixture-owner", "VERIFYING", null, reserved.name)
      expect(runs.complete(data.plan.id, "fixture-owner", result).state).toBe("SUCCEEDED")
      const events = data.store.db.query("SELECT payload FROM events WHERE kind='refinement_quality_evaluated'")
        .all() as { payload: string }[]
      expect(events).toHaveLength(1)
      expect(JSON.parse(events[0].payload)).toMatchObject({ quality_counts: { passed: 0, rejected: 2, not_evaluated: 0 },
        engine_success_count: 2, engine_failed_count: 0, hits_confirmed: false })
    } finally { data.store.close() }
    const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
    try {
      const runs = new RefinementRuns(new Refinements(reopened))
      expect(runs.get(data.plan.id).state).toBe("SUCCEEDED")
      expect(JSON.parse(runs.get(data.plan.id).manifest!)).toEqual(result)
      expect(runs.list()).toHaveLength(1)
      expect(() => runs.reserve(data.plan.id, "fixture-owner")).toThrow()
    } finally { reopened.close() }
  })
}

// Actual subprocess + files, explicitly not scientific evidence. The creation
// marker is exclusive so a repeated submit fails even across transport reopen.
function processTransport(directory: string, mode = "success", delay = 40, lostReply = false, classified = false) {
  const children: { child: ReturnType<typeof Bun.spawn>; settled: Promise<void> }[] = []
  const transport: RefinementTransport = {
    async prepare(plan) {
      await mkdir(directory, { recursive: true })
      const file = Bun.file(path.join(directory, "plan.json"))
      if (await file.exists()) {
        if (digest(await file.json()) !== digest(plan)) throw new ReviewRequired("Staged fixture plan changed")
        return
      }
      await Bun.write(file, JSON.stringify(plan))
    },
    async preflight(plan) {
      if (digest(await Bun.file(path.join(directory, "plan.json")).json()) !== digest(plan)) throw new ReviewRequired("Fixture preflight failed")
    },
    async submit(plan, run) {
      const file = await open(path.join(directory, "create-once.json"), "wx")
      await file.writeFile(JSON.stringify({ name: run.name, command_digest: run.command_digest }))
      await file.close()
      const child = Bun.spawn([process.execPath, path.join(import.meta.dir, "refinement-process.fixture.ts"),
        directory, JSON.stringify(classified ? classifiedFixture(plan, 0) : resultFixture(plan)), mode, String(delay)], { stdin: "ignore", stdout: "ignore", stderr: "ignore" })
      await Bun.write(path.join(directory, "observed.json"), JSON.stringify({ name: run.name, state: "running" }))
      const settled = child.exited.then(async (code) => {
        await Bun.write(path.join(directory, "observed.json"), JSON.stringify({ name: run.name,
          state: child.signalCode ? "cancelled" : code === 0 ? "succeeded" : "failed" }))
      })
      children.push({ child, settled })
      if (lostReply) throw new Error("Fixture create response lost after actual child launch")
      return { handle: run.name }
    },
    async observe(_plan, run) {
      const file = Bun.file(path.join(directory, "observed.json"))
      if (!await file.exists()) return { state: "missing" }
      const observed = await file.json()
      if (observed.name !== run.name) throw new ReviewRequired("Fixture observed name changed")
      return { state: observed.state, handle: run.name }
    },
    async stop() { for (const { child } of children) child.kill("SIGTERM") },
    async verify() {
      const result = await Bun.file(path.join(directory, "result.json")).json()
      for (const [name, expected] of Object.entries(result.artifacts)) {
        const hash = new Bun.CryptoHasher("sha256").update(await Bun.file(path.join(directory, name)).arrayBuffer()).digest("hex")
        if (hash !== expected) throw new ReviewRequired("Actual fixture output hash changed")
      }
      return result
    },
  }
  return { transport, async finished() { await Promise.all(children.map((entry) => entry.settled)) },
    count: () => children.length, async cleanup() { for (const { child } of children) child.kill("SIGTERM"); await Promise.all(children.map((entry) => entry.settled)) } }
}

function approveFixture(data: Awaited<ReturnType<typeof setup>>) {
  data.refinements.create(data.plan)
  data.refinements.approve(data.plan.id, digest(data.plan), { directory: data.plan.directory, sessionID: data.plan.session,
    abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
  return new RefinementRuns(data.refinements)
}

async function cliFixture(directory: string, engine = "af3", classified = false, quality = false) {
  const cli = path.join(directory, "fixture-inspire.ts")
  await Bun.write(cli, `#!${process.execPath}\n` + await Bun.file(path.join(import.meta.dir, "refinement-cli.fixture.ts")).text())
  await chmod(cli, 0o700)
  const initial = await setup(directory, cli, engine, classified)
  const data = quality ? { ...initial, plan: qualityFixture(initial, "af3").plan } : initial
  const settings = { spec: data.plan.resource, name: `${data.plan.id}-${engine}` }
  const configure = (options = {}) => Bun.write(cli + ".json", JSON.stringify({ ...settings, ...options }))
  await configure()
  const calls = async (): Promise<string[][]> => await Bun.file(cli + ".calls").exists()
    ? (await Bun.file(cli + ".calls").text()).trim().split("\n").map((line) => JSON.parse(line)) : []
  const runs = approveFixture(data)
  const evidence = processTransport(path.join(directory, "local-evidence-only"))
  const transport = new RefinementInspire(runs, new Inspire(data.task.plan.connection), evidence.transport)
  return { ...data, runs, transport, configure, calls, evidence }
}

function stagingFixture(data: Awaited<ReturnType<typeof setup>>): ReturnType<typeof verifyPreparation> {
  const plan = data.plan
  return { version: "refinement-staging-v1", id: plan.id, digest: digest(plan), code: plan.code,
    selection_id: plan.selection_id, selection_manifest_digest: plan.selection_manifest_digest,
    input_count: plan.input.candidates.length,
    path: `${data.task.plan.runtime.root}/tasks/${plan.parent_id}/refinements/${plan.id}/plan.json`,
    output: refinementCommand(plan, data.task).output,
    runtime_verified: false, resource_approval_granted: false, staged: true,
    plan_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify(canonical({ plan, digest: digest(plan) })) + "\n").digest("hex") }
}

function readbackFixture(data: Awaited<ReturnType<typeof setup>>): ReturnType<typeof verifyReadback> {
  const plan = data.plan
  return { version: "refinement-readback-v1", id: plan.id, digest: digest(plan), code: plan.code,
    selection_id: plan.selection_id, selection_manifest_digest: plan.selection_manifest_digest,
    output: refinementCommand(plan, data.task).output, manifest_sha256: "9".repeat(64),
    evidence: { version: "refinement-artifact-evidence-v1", engine: plan.engine.name,
      input_contract: { input_count: plan.input.candidates.length, input_sha256: plan.input.sha256,
        identity_sha256: plan.input.identity_sha256, verified: true, grants_resource_approval: false },
      candidates: plan.input.candidates.map((row, index) => ({ input_sdf_index: row.input_sdf_index, compound_id: row.compound_id,
        engine_status: "SUCCESS", score: index + 1, structure: { path: `fixture-${index}.cif`, pose_index: null }, failure_reason: null })),
      artifacts: { "fixture-0.cif": "1".repeat(64), "fixture-1.cif": "2".repeat(64) }, score_direction: "lower_is_better",
      scientific_quality_verified: false, resource_approval_granted: false } }
}

// Contract fixtures only, never predictions or evidence of an actual SDK run.
function qualityFixture(data: Awaited<ReturnType<typeof setup>>, engine: "af3" | "covdock", strict = false) {
  const policy = strict ? provisionalRefinementQuality : defaultRefinementQuality
  const plan = refinementPlan.parse({ ...(engine === "af3" ? data.plan : workflowFixture(data).stages[1]),
    result_contract: "screening-refinement-outcome-v2", quality_policy: policy,
    ...(engine === "af3" ? { geometry_contract: "af3-geometry-evidence-v1" } : {}) })
  const receipt = readbackFixture({ ...data, plan })
  if (engine === "covdock") receipt.evidence.candidates.forEach((row, index) => { row.structure!.pose_index = index })
  receipt.quality = { version: "refinement-quality-measurements-v1", policy_digest: digest(policy),
    candidates: receipt.evidence.candidates.map((row) => ({ input_sdf_index: row.input_sdf_index, compound_id: row.compound_id,
      evidence: [row.structure!.path], metrics: engine === "af3" ? { engine, mpae: row.score!, iptm: .7, ligand_plddt_mean: 75, has_clash: false }
        : { engine, covalent_link_present: true, target_atom_match_count: 1, external_target_bond_count: 1,
          geometry_inspection_status: "SUCCESS", ligand_heavy_atom_count: 5, covalent_bond_length_angstrom: 1.8,
          minimum_nonbonded_heavy_atom_distance_angstrom: 2.5, severe_clash: false, strain_kcal_mol: 10,
          ...(strict ? { strain_provenance: { source_field: "r_strain_Local_Strain_Energy" as const,
            unit: "kcal/mol" as const, definition: "vendor_local_strain_unmodified" as const, status: "VALID" as const } } : {}) } })) }
  if (engine === "af3") receipt.geometry = { version: "af3-geometry-evidence-v1", input_sha256: plan.input.sha256,
    target_sha256: plan.assets[refinementPaths(plan.runtime, data.task.plan.runtime.root, plan.code.release_id).target],
    scientific_quality_verified: false, candidates: receipt.evidence.candidates.map((row) => ({ input_sdf_index: row.input_sdf_index,
      compound_id: row.compound_id, state: "MEASURED", measurements: { source_residue: 797, af3_sequence_residue: 105,
        nucleophilic_atom: "SG", electrophilic_atom: "C3", reactive_distance_angstrom: 1.8, model_target_covalent_link_count: 1,
        model_has_exact_covalent_link: true, ligand_heavy_atom_count: 5, protein_heavy_atom_count: 1, nonbonded_pair_count: 4,
        minimum_nonbonded_heavy_atom_distance_angstrom: 4.5, distance_scope: "protein_ligand_heavy_atoms_excluding_declared_reactive_pair",
        full_topology_and_stereochemistry_verified: false, scientific_quality_verified: false } })) }
  return { plan, receipt }
}

test("provisional QC separates unknown evidence, observed failures and execution", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const { plan, receipt } = qualityFixture(data, "covdock", true)
    const argv = refinementCommand(plan, data.task).argv
    expect(argv).toContain("--gate-algorithm-version")
    expect(argv).toContain("covdock-lo-pose-gate-v3.0-provisional")
    const evaluate = (value: unknown) => evaluateRefinementQuality(plan, data.task, value)
    const normal = evaluate(receipt)
    expect(normal.candidates[0].quality.assessment?.physical_validity).toBe("PASS")
    for (const [changes, physical, complete] of [
      [{ severe_clash: null }, "UNKNOWN", false],
      [{ severe_clash: true }, "FAIL", true],
      [{ severe_clash: true, target_atom_match_count: null }, "FAIL", false],
      [{ covalent_link_present: false, target_atom_match_count: null }, "FAIL", false],
      [{ geometry_inspection_status: "FAILED", severe_clash: true }, "UNKNOWN", false],
      [{ strain_kcal_mol: null, strain_provenance: { source_field: "r_strain_Local_Strain_Energy",
        unit: "kcal/mol", definition: "vendor_local_strain_unmodified", status: "MISSING" } }, "UNKNOWN", false],
    ] as const) {
      const result = evaluate({ ...receipt, quality: { ...receipt.quality,
        candidates: receipt.quality!.candidates.map((row) => ({ ...row, metrics: { ...row.metrics, ...changes } })) } })
      expect(result.success_count).toBe(2)
      expect(result.candidates[0].quality.assessment).toMatchObject({
        physical_validity: physical, evidence_complete: complete, review_eligible: false,
        calibration_status: "provisional",
      })
      expect(result.candidates[0].quality.status).toBe("REJECTED")
    }
    expect(() => evaluate({ ...receipt, quality: { ...receipt.quality,
      candidates: receipt.quality!.candidates.map((row) => ({ ...row, metrics: { ...row.metrics, strain_provenance: undefined } })) } })).toThrow("provenance")
    expect(() => evaluate({ ...receipt, quality: { ...receipt.quality,
      candidates: receipt.quality!.candidates.map((row) => ({ ...row, metrics: { ...row.metrics, strain_kcal_mol: null } })) } })).toThrow("provenance")
    const altered = structuredClone(normal)
    altered.candidates[0].quality.assessment!.physical_validity = "UNKNOWN"
    expect(() => verifyRefinementResult(altered, plan)).toThrow("Independent quality axes")
    const missing = structuredClone(normal)
    delete missing.candidates[0].quality.assessment
    expect(() => verifyRefinementResult(missing, plan)).toThrow("Independent quality axes")
  } finally { data.store.close() }
})

test("provisional AF3 QC preserves independent axes without new affinity thresholds", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const { plan, receipt } = qualityFixture(data, "af3", true)
    const result = evaluateRefinementQuality(plan, data.task, receipt)
    expect(result.candidates.every((row) => row.quality.assessment?.physical_validity === "PASS")).toBe(true)
    expect(refinementCommand(plan, data.task).argv).not.toContain("--gate-algorithm-version")
    const script = path.resolve(import.meta.dir, "../../../../../../science/src/covscreen/refinement_quality.py")
    const python = Bun.spawnSync(["python3", "-I", "-B", "-c",
      "import json,runpy,sys; print(json.dumps(runpy.run_path(sys.argv[1])['STRICT_POLICY']))", script])
    expect(python.exitCode).toBe(0)
    expect(JSON.parse(python.stdout.toString())).toEqual(provisionalRefinementQuality)
  } finally { data.store.close() }
})

test("host and released Python structural QC policies are identical without importing an engine", async () => {
  const script = path.resolve(import.meta.dir, "../../../../../../science/src/covscreen/refinement_quality.py")
  const result = Bun.spawnSync(["python3", "-I", "-B", script])
  expect(result.exitCode).toBe(0)
  expect(JSON.parse(result.stdout.toString())).toEqual(defaultRefinementQuality)
})

for (const engine of ["af3", "covdock"] as const) test(`fixed ${engine} structural QC preserves evidence and rejects missing or altered policy`, async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const { plan, receipt } = qualityFixture(data, engine)
    const evaluate = (value: unknown) => evaluateRefinementQuality(plan, data.task, value)
    const result = evaluate(receipt)
    expect(result.version).toBe("screening-refinement-outcome-v2")
    if (result.version !== "screening-refinement-outcome-v2") throw new Error("Wrong QC contract")
    expect(result.quality_counts).toEqual({ passed: 2, rejected: 0, not_evaluated: 0 })
    expect(result.quality_policy_digest).toBe(digest(defaultRefinementQuality))
    expect(result.artifacts).toEqual(receipt.evidence.artifacts)
    expect(receipt.evidence.scientific_quality_verified).toBe(false)
    expect(() => evaluate({ ...receipt, quality: undefined })).toThrow("quality policy")
    for (const change of [{ policy_digest: "0".repeat(64) }, { candidates: [] },
      { candidates: receipt.quality!.candidates.map((row) => ({ ...row, evidence: ["unobserved.json"] })) },
      { candidates: receipt.quality!.candidates.map((row) => ({ ...row, compound_id: "substituted" })) }]) {
      expect(() => evaluate({ ...receipt, quality: { ...receipt.quality, ...change } })).toThrow()
    }
    expect(() => refinementPlan.parse({ ...plan, quality_policy: { ...defaultRefinementQuality, covdock_max_strain_kcal_mol: 999 } })).toThrow()
    expect(() => verifyRefinementResult({ ...result, quality_policy_digest: "0".repeat(64) }, plan)).toThrow("structural QC policy")
    expect(() => refinementPlan.parse({ ...plan, result_contract: undefined })).toThrow("classified")
  } finally { data.store.close() }
})

test("AF3 QC retains confidence without inventing affinity cutoffs and rejects observed geometry defects", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const { plan, receipt } = qualityFixture(data, "af3")
    const evaluate = (value: unknown) => evaluateRefinementQuality(plan, data.task, value)
    // Deliberately weak confidence is reported, not calibrated into hit activity.
    const weak = { ...receipt, quality: { ...receipt.quality, candidates: receipt.quality!.candidates.map((row) => ({ ...row,
      metrics: { ...row.metrics, iptm: .01, ligand_plddt_mean: 1 } })) } }
    expect(evaluate(weak).candidates.every((row) => "quality" in row && row.quality.status === "PASSED")).toBe(true)
    for (const change of [{ reactive_distance_angstrom: 1.49 }, { reactive_distance_angstrom: 2.21 },
      { model_target_covalent_link_count: 0, model_has_exact_covalent_link: false },
      { minimum_nonbonded_heavy_atom_distance_angstrom: 1.19 }]) {
      const result = evaluate({ ...receipt, geometry: { ...receipt.geometry,
        candidates: receipt.geometry!.candidates.map((row) => ({ ...row, measurements: { ...row.measurements, ...change } })) } })
      expect(result.success_count).toBe(2)
      expect(result.candidates.every((row) => "quality" in row && row.quality.status === "REJECTED")).toBe(true)
    }
    const clash = evaluate({ ...receipt, quality: { ...receipt.quality, candidates: receipt.quality!.candidates.map((row) => ({ ...row,
      metrics: { ...row.metrics, has_clash: true } })) } })
    expect(clash.candidates.every((row) => "quality" in row && row.quality.reason?.includes("af3_reported_clash"))).toBe(true)
  } finally { data.store.close() }
})

test("CovDock QC cannot pass missing strain, ambiguous bonds, clashes or out-of-range distances", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const { plan, receipt } = qualityFixture(data, "covdock")
    for (const change of [{ strain_kcal_mol: null }, { strain_kcal_mol: 20.01 }, { covalent_link_present: false },
      { geometry_inspection_status: null }, { severe_clash: null }, { severe_clash: true }, { external_target_bond_count: 2 },
      { ligand_heavy_atom_count: 0 }, { covalent_bond_length_angstrom: 2.3 }, { minimum_nonbonded_heavy_atom_distance_angstrom: null }]) {
      const result = evaluateRefinementQuality(plan, data.task, { ...receipt, quality: { ...receipt.quality,
        candidates: receipt.quality!.candidates.map((row) => ({ ...row, metrics: { ...row.metrics, ...change } })) } })
      expect(result.success_count).toBe(2)
      expect(result.candidates.every((row) => "quality" in row && row.quality.status === "REJECTED")).toBe(true)
    }
  } finally { data.store.close() }
})

test("geometry readback is explicitly approved, bound to original input/target, and never scientific acceptance", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const plan = refinementPlan.parse({ ...data.plan, geometry_contract: "af3-geometry-evidence-v1" })
    const receipt = readbackFixture({ ...data, plan })
    const target = refinementPaths(plan.runtime, data.task.plan.runtime.root, plan.code.release_id).target
    const geometry: NonNullable<typeof receipt.geometry> = { version: "af3-geometry-evidence-v1", input_sha256: plan.input.sha256,
      target_sha256: plan.assets[target], scientific_quality_verified: false,
      candidates: receipt.evidence.candidates.map((row) => ({ input_sdf_index: row.input_sdf_index, compound_id: row.compound_id,
        state: "MEASURED", measurements: { source_residue: 797, af3_sequence_residue: 105, nucleophilic_atom: "SG", electrophilic_atom: "C3",
          reactive_distance_angstrom: 1.8, model_target_covalent_link_count: 1, model_has_exact_covalent_link: true,
          ligand_heavy_atom_count: 5, protein_heavy_atom_count: 1, nonbonded_pair_count: 4,
          minimum_nonbonded_heavy_atom_distance_angstrom: 4.5,
          distance_scope: "protein_ligand_heavy_atoms_excluding_declared_reactive_pair",
          full_topology_and_stereochemistry_verified: false, scientific_quality_verified: false } })) }
    const verify = (value: unknown) => verifyReadback(value, plan, data.task)
    expect(() => verify(receipt)).toThrow("geometry contract")
    expect(verify({ ...receipt, geometry }).geometry).toEqual(geometry)
    expect(() => verifyReadback({ ...receipt, digest: digest(data.plan), geometry }, data.plan, data.task)).toThrow("geometry contract")
    for (const change of [{ input_sha256: "0".repeat(64) }, { target_sha256: "0".repeat(64) },
      { scientific_quality_verified: true }, { candidates: [] }, { candidates: [geometry.candidates[0], geometry.candidates[0]] },
      ...[{ af3_sequence_residue: 773 }, { model_target_covalent_link_count: 2 }, { nonbonded_pair_count: 5 },
        { minimum_nonbonded_heavy_atom_distance_angstrom: null }, { full_topology_and_stereochemistry_verified: true },
        { reactive_distance_angstrom: 0 }].map((metrics) => ({ candidates: geometry.candidates.map((row) => ({ ...row,
          measurements: { ...row.measurements, ...metrics } })) })),
    ]) expect(() => verify({ ...receipt, geometry: { ...geometry, ...change } })).toThrow()
    const negative = { ...geometry, candidates: geometry.candidates.map((row) => ({ ...row, measurements: { ...row.measurements!,
      model_target_covalent_link_count: 0, model_has_exact_covalent_link: false } })) }
    expect(verify({ ...receipt, geometry: negative }).geometry?.candidates[0].measurements?.model_has_exact_covalent_link).toBe(false)
    expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { data.store.close() }
})

async function readbackSetup(directory: string, classified = false, quality = false) {
  const data = await cliFixture(directory, "af3", classified, quality)
  const staging = stagingFixture(data)
  const readback = quality ? qualityFixture(data, "af3").receipt : readbackFixture(data)
  await data.configure({ staging, readback })
  data.runs.ensure(data.plan.id)
  data.runs.acquire(data.plan.id, "readback-owner")
  const preparation = new RefinementPreparation(data.runs, new Inspire(data.task.plan.connection))
  await preparation.prepare(data.plan, data.runs.get(data.plan.id))
  const reserved = data.runs.reserve(data.plan.id, "readback-owner")
  data.runs.update(data.plan.id, "readback-owner", "VERIFYING", null, reserved.name)
  return { ...data, staging, readback, preparation, readbacks: new RefinementReadbacks(preparation) }
}

for (const code of ["CommandFailed", "remote_worker_failed", "ConnectionInterrupted"]) {
  test(`real CLI readback failure distinguishes deterministic rejection from transport interruption: ${code}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await readbackSetup(tmp.path, true, true)
    try {
      await data.configure({ staging: data.staging, readback: data.readback, readback_error: code })
      const error = await data.readbacks.read(data.plan, data.runs.get(data.plan.id)).then(() => null, (error: Error) => error)
      expect(error).toBeInstanceOf(Error)
      expect(error instanceof ReviewRequired).toBe(code !== "ConnectionInterrupted")
      // The CLI fixture fails on its second read: preserve the first witness,
      // but never promote it to a completed scientific outcome.
      expect(data.readbacks.saved(data.plan)).toEqual(data.readback)
      expect(data.runs.get(data.plan.id).manifest).toBeNull()
      expect((await data.calls()).filter((args) => args[5] === "create")).toHaveLength(0)
    } finally { data.store.close() }
  })
}

test("readback binds every artifact identity and cannot masquerade as scientific acceptance", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const receipt = readbackFixture(data)
    const files = verifyReadback(receipt, data.plan, data.task)
    expect(files.evidence.scientific_quality_verified).toBe(false)
    for (const changed of [{ ...receipt, output: "/outside" }, { ...receipt, digest: "0".repeat(64) },
      { ...receipt, evidence: { ...receipt.evidence, scientific_quality_verified: true } },
      { ...receipt, evidence: { ...receipt.evidence, candidates: receipt.evidence.candidates.slice(1) } },
      { ...receipt, evidence: { ...receipt.evidence, artifacts: {} } },
      { ...receipt, evidence: { ...receipt.evidence, candidates: receipt.evidence.candidates.map((row) => ({ ...row, score: -1 })) } }]) {
      expect(() => verifyReadback(changed, data.plan, data.task)).toThrow()
    }
    const result = { ...resultFixture(data.plan), artifacts: files.evidence.artifacts }
    expect(verifyReadbackOutcome(result, data.plan, files).success_count).toBe(2)
    expect(() => verifyReadbackOutcome({ ...result, artifacts: { ...result.artifacts, "fixture-0.cif": "0".repeat(64) } }, data.plan, files)).toThrow("artifacts")
    expect(() => verifyReadbackOutcome({ ...result, candidates: result.candidates.map((row) => ({ ...row, score: 99 })) }, data.plan, files)).toThrow("substituted")
    expect(() => verifyReadbackOutcome({ ...result, gates: { ...result.gates, scientific_quality: false } }, data.plan, files)).toThrow()
  } finally { data.store.close() }
})

test("approved structural QC uses the fixed production decision even when a legacy callback would pass anything", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path, true, true)
  try {
    const changed = { ...data.readback, quality: { ...data.readback.quality,
      candidates: data.readback.quality!.candidates.map((row) => ({ ...row, metrics: { ...row.metrics, has_clash: true } })) } }
    await data.configure({ staging: data.staging, readback: changed })
    const calls: string[] = []
    const transport = refinementTransport(data.runs, new Inspire(data.task.plan.connection), {
      preflight: async () => { calls.push("unused-in-readback") },
      verify: async () => { calls.push("unsafe-legacy-verdict"); return classifiedFixture(data.plan) },
    })
    const result = verifyRefinementResult(await transport.verify(data.plan, data.runs.get(data.plan.id)), data.plan)
    expect(result.success_count).toBe(2)
    if (result.version !== "screening-refinement-outcome-v2") throw new Error("Wrong result contract")
    expect(result.quality_counts).toEqual({ passed: 0, rejected: 2, not_evaluated: 0 })
    expect(result.candidates.every((row) => row.quality.reason === "af3_reported_clash")).toBe(true)
    expect(calls).toEqual([])
    expect((await data.calls()).filter((argv) => ["job", "hpc"].includes(argv[0]) && argv[1] === "create")).toEqual([])
  } finally { data.store.close() }
})

test("classified quality verdicts retain fresh engine outcomes and cannot cite unobserved artifacts", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path, "/never-invoked", "af3", true)
  try {
    const files = verifyReadback(readbackFixture(data), data.plan, data.task)
    const result = classifiedFixture(data.plan)
    expect(digest(verifyReadbackOutcome(result, data.plan, files))).toBe(digest(result))
    expect(files.evidence.scientific_quality_verified).toBe(false)
    const extra = { ...result, artifacts: { ...result.artifacts, "unobserved-quality.json": "a".repeat(64) },
      candidates: result.candidates.map((row) => ({ ...row, quality: { ...row.quality, evidence: ["unobserved-quality.json"] } })) }
    expect(() => verifyReadbackOutcome(extra, data.plan, files)).toThrow("outside the freshly verified inventory")
    expect(() => verifyReadbackOutcome({ ...result,
      candidates: result.candidates.map((row) => ({ ...row, score: 999 })),
    }, data.plan, files)).toThrow("substituted")
    const failed = verifyReadback({ ...files, evidence: { ...files.evidence,
      candidates: files.evidence.candidates.map((row, index) => index ? row : { ...row,
        engine_status: "FAILED", score: null, structure: null, failure_reason: "Fixture engine error" }),
    } }, data.plan, data.task)
    expect(() => verifyReadbackOutcome(result, data.plan, failed)).toThrow("substituted")
    const historical = refinementPlan.parse({ ...data.plan, result_contract: undefined })
    const old = { ...resultFixture(historical), artifacts: files.evidence.artifacts }
    expect(verifyRefinementResult(old, historical)).not.toHaveProperty("quality_counts")
    expect(() => verifyRefinementResult({ ...old, quality_counts: result.quality_counts }, historical)).toThrow()
    expect(() => verifyRefinementResult({ ...result, plan_digest: digest(historical) }, historical)).toThrow("result contract")
    expect(() => verifyRefinementResult({ ...old, plan_digest: digest(data.plan) }, data.plan)).toThrow("result contract")
  } finally { data.store.close() }
})

test("fixed transport can retain a verified zero-pass outcome without relabelling the engine evidence", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path, true)
  try {
    const transport = refinementTransport(data.runs, new Inspire(data.task.plan.connection), {
      async preflight() {},
      // Explicit local contract fixture, not a production scientific validator.
      async verify(plan, _run, files) { return { ...classifiedFixture(plan), artifacts: files.evidence.artifacts } },
    })
    const outcome = await transport.verify(data.plan, data.runs.get(data.plan.id))
    const run = data.runs.complete(data.plan.id, "readback-owner", outcome)
    expect(run.state).toBe("SUCCEEDED")
    expect(JSON.parse(run.manifest!).quality_counts).toEqual({ passed: 0, rejected: 2, not_evaluated: 0 })
    expect(data.readbacks.saved(data.plan)?.evidence.candidates.every((row) => row.engine_status === "SUCCESS")).toBe(true)
    expect((await data.calls()).every((args) => !args.includes("create"))).toBe(true)
  } finally { data.store.close() }
})

test("retained output witness survives SQLite reopen and subsequent reads use its exact checksum", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path)
  try {
    expect(await data.readbacks.read(data.plan, data.runs.get(data.plan.id))).toEqual(data.readback)
    expect(data.runs.get(data.plan.id).state).toBe("VERIFYING")
  } finally { data.store.close() }
  const store = new Store(path.join(tmp.path, "refinement.sqlite"))
  try {
    const runs = new RefinementRuns(new Refinements(store))
    const reader = new RefinementReadbacks(new RefinementPreparation(runs, new Inspire(data.task.plan.connection)))
    expect(await reader.read(data.plan, runs.get(data.plan.id))).toEqual(data.readback)
    const packets = (await data.calls()).map((args) => JSON.parse(Buffer.from(args.at(-1)!.match(/'([A-Za-z0-9+/=]+)'$/)![1], "base64").toString()))
    expect(packets.filter((packet) => packet.action === "outputs").map((packet) => packet.evidence_sha256))
      .toEqual([null, data.readback.manifest_sha256, data.readback.manifest_sha256])
    expect(runs.get(data.plan.id).state).toBe("VERIFYING")
  } finally { store.close() }
})

test("uncertain readback retains first witness and cannot replace it with changed results", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path)
  try {
    await data.configure({ staging: data.staging, readback: data.readback, readback_error: "ConnectionInterrupted" })
    await expect(data.readbacks.read(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("ConnectionInterrupted")
    expect(data.readbacks.saved(data.plan)).toEqual(data.readback)
    await data.configure({ staging: data.staging, readback: { ...data.readback, manifest_sha256: "0".repeat(64) } })
    await expect(data.readbacks.read(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("witness changed")
    expect(data.readbacks.saved(data.plan)).toEqual(data.readback)
    expect(data.runs.get(data.plan.id).state).toBe("VERIFYING")
    expect((await data.calls()).every((args) => args[4] === "notebook")).toBe(true)
  } finally { data.store.close() }
})

test("readback refuses cancellation and changed identity before reading remote artifacts", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path)
  try {
    const run = data.runs.get(data.plan.id)
    const count = (await data.calls()).length
    await expect(data.readbacks.read({ ...data.plan, max_points: 0.3 }, run)).rejects.toThrow("Unapproved")
    data.runs.cancel(data.plan.id, tmp.path)
    await expect(data.readbacks.read(data.plan, run)).rejects.toThrow("reconciled completed job")
    expect(await data.calls()).toHaveLength(count)
    expect(data.readbacks.saved(data.plan)).toBeNull()
  } finally { data.store.close() }
})

test("fixed transport passes fresh files to independent quality validation and rejects substituted outcomes", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path)
  try {
    const received: unknown[] = []
    const transport = refinementTransport(data.runs, new Inspire(data.task.plan.connection), {
      async preflight() {},
      async verify(plan, _run, files) {
        received.push(files)
        return { ...resultFixture(plan), artifacts: files.evidence.artifacts }
      },
    })
    const result = await transport.verify(data.plan, data.runs.get(data.plan.id))
    expect(received).toEqual([data.readback])
    expect(verifyRefinementResult(result, data.plan).success_count).toBe(2)
    // Returning a synthetic quality result in this isolated test does not mark
    // the fixture run successful; real production validators are still missing.
    expect(data.runs.get(data.plan.id).state).toBe("VERIFYING")
  } finally { data.store.close() }
})

test("outputs changed during quality validation cannot become an accepted outcome", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await readbackSetup(tmp.path)
  try {
    const transport = refinementTransport(data.runs, new Inspire(data.task.plan.connection), {
      async preflight() {},
      async verify(plan, _run, files) {
        await data.configure({ staging: data.staging, readback: { ...data.readback, manifest_sha256: "0".repeat(64) } })
        return { ...resultFixture(plan), artifacts: files.evidence.artifacts }
      },
    })
    await expect(transport.verify(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("witness changed")
    expect(data.runs.get(data.plan.id).state).toBe("VERIFYING")
  } finally { data.store.close() }
})

test("staging receipt binds exact approved plan, code, selection, output and canonical file bytes", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const receipt = stagingFixture(data)
    expect(verifyPreparation(receipt, data.plan, data.task)).toEqual(receipt)
    for (const change of [{ id: "refine-" + "1".repeat(24) }, { digest: "0".repeat(64) },
      { code: { ...receipt.code, release_id: "0".repeat(64) } }, { selection_id: "0".repeat(64) },
      { selection_manifest_digest: "0".repeat(64) }, { input_count: 1 }, { path: "/outside/plan.json" },
      { output: "/outside/output" }, { runtime_verified: true }, { resource_approval_granted: true },
      { plan_sha256: "0".repeat(64) }, { staged: false }]) {
      expect(() => verifyPreparation({ ...receipt, ...change }, data.plan, data.task)).toThrow()
    }
    const args = await preparationArgs(data.plan, data.task, "read")
    expect(args.slice(0, 7)).toEqual(["notebook", "exec", "fixture", "--workspace", "CPU资源空间", "--timeout", "90"])
    expect(args).not.toContain("--stdin")
    const encoded = args.at(-1)!.match(/'([A-Za-z0-9+/=]+)'$/)![1]
    const packet = JSON.parse(Buffer.from(encoded, "base64").toString())
    expect(packet.payload).toBeNull()
    expect(packet.checker_sha256).toBe(data.plan.assets[`${data.task.plan.runtime.root}/releases/${data.plan.code.release_id}/code_release.py`])
    expect(packet.python_sha256).toBe(data.plan.assets[data.plan.runtime.python])
  } finally { data.store.close() }
})

test("bundled refinement preparation carries its verifier without a mutable Python sidecar", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const build = await Bun.build({ entrypoints: [path.join(import.meta.dir, "refinement-preparation.ts")],
      outdir: path.join(tmp.path, "bundle"), target: "bun", format: "esm", minify: true })
    expect(build.success).toBe(true)
    expect(build.outputs).toHaveLength(1)
    const compiled = await import(build.outputs[0].path) as Pick<typeof import("./refinement-preparation"), "preparationArgs">
    const sidecar = path.join(path.dirname(build.outputs[0].path), "stage_bootstrap.py")
    expect(await Bun.file(sidecar).exists()).toBe(false)
    for (const action of ["freeze", "read", "outputs"] as const) {
      const witness = action === "outputs" ? "a".repeat(64) : null
      expect(await compiled.preparationArgs(data.plan, data.task, action, witness))
        .toEqual(await preparationArgs(data.plan, data.task, action, witness))
    }
    // A file beside the deployed bundle must not replace the built verifier.
    // Only command generation is exercised here; neither Python nor CLI runs.
    await Bun.write(sidecar, "raise RuntimeError('untrusted adjacent source')\n")
    expect(await compiled.preparationArgs(data.plan, data.task, "read"))
      .toEqual(await preparationArgs(data.plan, data.task, "read"))
    expect(data.store.get(data.task.id).state).toBe("SUCCEEDED")
  } finally { data.store.close() }
})

test("approved staging persists a readback receipt; reopening uses read only and never creates compute", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  const receipt = stagingFixture(data)
  await data.configure({ staging: receipt })
  data.runs.ensure(data.plan.id)
  data.runs.acquire(data.plan.id, "staging-owner")
  const run = data.runs.get(data.plan.id)
  const preparation = new RefinementPreparation(data.runs, new Inspire(data.task.plan.connection))
  try {
    await expect(preparation.read(data.plan, run)).rejects.toThrow("No independently retained")
    expect(await data.calls()).toHaveLength(0)
    await preparation.prepare(data.plan, run)
    expect(preparation.saved(data.plan)).toEqual(receipt)
  } finally { data.store.close() }
  const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
  try {
    const runs = new RefinementRuns(new Refinements(reopened))
    const fresh = new RefinementPreparation(runs, new Inspire(data.task.plan.connection))
    await fresh.prepare(data.plan, runs.get(data.plan.id))
    expect(await fresh.read(data.plan, runs.get(data.plan.id))).toEqual(receipt)
    const calls = await data.calls()
    expect(calls).toHaveLength(5)
    const actions = calls.map((args) => {
      expect(args.slice(4, 6)).toEqual(["notebook", "exec"])
      const encoded = args.at(-1)!.match(/'([A-Za-z0-9+/=]+)'$/)![1]
      return JSON.parse(Buffer.from(encoded, "base64").toString()).action
    })
    expect(actions).toEqual(["freeze", "read", "read", "read", "read"])
    expect(runs.get(data.plan.id).submitted).toBeNull()
  } finally { reopened.close() }
})

test("staging rejects changed or failed remote evidence without retaining a successful receipt", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  try {
    const receipt = stagingFixture(data)
    data.runs.ensure(data.plan.id)
    data.runs.acquire(data.plan.id, "staging-owner")
    const run = data.runs.get(data.plan.id)
    const preparation = new RefinementPreparation(data.runs, new Inspire(data.task.plan.connection))
    for (const options of [{ staging_read: { ...receipt, plan_sha256: "0".repeat(64) } },
      { staging_code: 1 }, { staging_output: "not-json" }, { staging_output: "{}" }]) {
      await data.configure({ staging: receipt, ...options })
      await expect(preparation.prepare(data.plan, run)).rejects.toThrow()
      expect(preparation.saved(data.plan)).toBeNull()
    }
    expect(data.runs.get(data.plan.id).submitted).toBeNull()
    expect((await data.calls()).every((args) => args[4] === "notebook")).toBe(true)
  } finally { data.store.close() }
})

test("staging refuses changed account, approval, ownership and cancellation before any remote command", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  try {
    data.runs.ensure(data.plan.id)
    data.runs.acquire(data.plan.id, "staging-owner")
    const run = data.runs.get(data.plan.id)
    const preparation = new RefinementPreparation(data.runs, new Inspire(data.task.plan.connection))
    const wrong = new RefinementPreparation(data.runs, new Inspire({ ...data.task.plan.connection, account: "other" }))
    await expect(wrong.prepare(data.plan, run)).rejects.toThrow("Connection differs")
    await expect(preparation.prepare({ ...data.plan, max_points: 0.3 }, run)).rejects.toThrow("Unapproved")
    await expect(preparation.prepare(data.plan, { ...run, lease_owner: "other" })).rejects.toThrow("lease lost")
    data.runs.cancel(data.plan.id, tmp.path)
    await expect(preparation.prepare(data.plan, run)).rejects.toThrow("Only unsubmitted")
    expect(await data.calls()).toHaveLength(0)
  } finally { data.store.close() }
})

test("production composition rechecks retained preparation before runtime and outcome validators", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  try {
    const receipt = stagingFixture(data)
    await data.configure({ staging: receipt })
    data.runs.ensure(data.plan.id)
    data.runs.acquire(data.plan.id, "staging-owner")
    const run = data.runs.get(data.plan.id)
    const calls: string[] = []
    const transport = refinementTransport(data.runs, new Inspire(data.task.plan.connection), {
      async preflight() { calls.push("runtime") }, async verify() { calls.push("scientific"); return resultFixture(data.plan) },
    })
    await transport.prepare(data.plan, run)
    await transport.preflight(data.plan, run)
    expect(calls).toEqual(["runtime"])
    await data.configure({ staging: { ...receipt, plan_sha256: "0".repeat(64) } })
    await expect(transport.preflight(data.plan, run)).rejects.toThrow("Prepared remote plan differs")
    expect(calls).toEqual(["runtime"])
    const reserved = data.runs.reserve(data.plan.id, "staging-owner")
    data.runs.update(data.plan.id, "staging-owner", "VERIFYING", null, reserved.name)
    await expect(transport.verify(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("Prepared remote plan differs")
    expect(calls).toEqual(["runtime"])
    expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(0)
  } finally { data.store.close() }
})

for (const engine of ["af3", "covdock"]) {
  test(`InspireSkill refinement ${engine} uses actual CLI subprocess, exact resource vectors and observed cancellation`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await cliFixture(tmp.path, engine)
    try {
      const runner = new RefinementRunner(data.runs, data.transport)
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("SUBMITTED")
      const calls = await data.calls()
      const creations = calls.filter((args) => args[5] === "create")
      expect(creations).toHaveLength(2) // One dry-run, one actual simulator create.
      expect(creations[0]).toContain("--dry-run")
      expect(creations[1]).not.toContain("--dry-run")
      const option = (flag: string) => creations[1][creations[1].indexOf(flag) + 1]
      expect(option("--name")).toBe(`${data.plan.id}-${engine}`)
      for (const [flag, value] of [["--workspace", data.plan.resource.workspace], ["--project", "fixture"],
        ["--group", data.plan.resource.group], ["--quota", data.plan.resource.quota], ["--image", "fixture"]]) expect(option(flag)).toBe(value)
      expect(creations[1]).toContain("--no-enable-notification")
      if (engine === "covdock") {
        expect(option("--entrypoint")).toStartWith("srun bash -lc '")
        expect(option("--number-of-tasks")).toBe("1")
        expect(option("--cpus-per-task")).toBe("4")
        expect(option("--memory-per-cpu")).toBe("3")
      } else {
        expect(creations[1]).toContain("--no-auto-fault-tolerance")
        expect(option("--nodes")).toBe("1")
        expect(option("--command")).toBe(data.runs.get(data.plan.id).command.shell)
      }
      data.runs.cancel(data.plan.id, tmp.path)
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("CANCEL_REQUESTED")
      expect((await data.calls()).filter((args) => args[5] === "stop")).toHaveLength(1)
      await data.configure({ status: "STOPPED" })
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("CANCELLED")
      expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(1)
      expect(data.evidence.count()).toBe(0) // No scientific fixture engine or remote computation started.
    } finally { data.store.close() }
  })
}

test("InspireSkill uncertain refinement create persists across SQLite reopen and cannot replay", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
  try {
    await data.configure({ lost: true })
    await new RefinementRunner(data.runs, data.transport).tick(data.plan.id)
    expect(data.runs.get(data.plan.id).state).toBe("DISPATCHING")
    const runs = new RefinementRuns(new Refinements(reopened))
    const transport = new RefinementInspire(runs, new Inspire(data.task.plan.connection), data.evidence.transport)
    const owner = transport.check(data.plan.id)!.owner
    expect(runs.acquire(data.plan.id, owner)).toBe(true)
    await expect(transport.submit(data.plan, runs.get(data.plan.id))).rejects.toThrow("unique dispatch")
    runs.release(data.plan.id, owner)
    await data.configure({ namespace: true })
    await new RefinementRunner(runs, transport).tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("RUNNING")
    expect(runs.get(data.plan.id).handle).toBe(`${data.plan.id}-af3`)
    expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(1)
  } finally { reopened.close(); data.store.close() }
})

test("InspireSkill refinement rejects missing preflight, changed account, and changed live price before dispatch", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  try {
    data.runs.ensure(data.plan.id)
    data.runs.acquire(data.plan.id, "fixture-owner")
    await expect(new RefinementInspire(data.runs, new Inspire({ ...data.task.plan.connection, account: "other" }), data.evidence.transport)
      .prepare(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("Connection differs")
    expect(await data.calls()).toHaveLength(0)
    await data.transport.prepare(data.plan, data.runs.get(data.plan.id))
    await data.configure({ price: 99 })
    await expect(data.transport.preflight(data.plan, data.runs.get(data.plan.id))).rejects.toThrow("price changed")
    const reserved = data.runs.reserve(data.plan.id, "fixture-owner")
    await expect(data.transport.submit(data.plan, reserved)).rejects.toThrow("preflight")
    expect((await data.calls()).filter((args) => args[5] === "create")).toHaveLength(0)
  } finally { data.store.close() }
})

test("InspireSkill missing, transient and mismatched observations do not cancel or recreate refinement", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path)
  try {
    const runner = new RefinementRunner(data.runs, data.transport)
    await runner.tick(data.plan.id)
    data.runs.cancel(data.plan.id, tmp.path)
    for (const options of [{ error: "NotFound" }, { error: "TransportTimeout" }, { override: { name: "another-task" } }]) {
      await data.configure(options)
      await runner.tick(data.plan.id)
      expect(data.runs.get(data.plan.id).state).toBe("CANCEL_REQUESTED")
    }
    expect((await data.calls()).filter((args) => args[5] === "stop")).toHaveLength(0)
    expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(1)
  } finally { data.store.close() }
})

test("InspireSkill HPC requires a completed Slurm step and exact observed resources before verification", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await cliFixture(tmp.path, "covdock")
  try {
    const runner = new RefinementRunner(data.runs, data.transport)
    await runner.tick(data.plan.id)
    await data.configure({ status: "SUCCEEDED", steps: "0/0" })
    await runner.tick(data.plan.id)
    expect(data.runs.get(data.plan.id).state).toBe("SUBMITTED")
    expect(data.runs.get(data.plan.id).manifest).toBeNull()
    await data.configure({ override: { resource: { gpu: 0, cpu: 8, memory_gib: 16, nodes: 1 } } })
    await runner.tick(data.plan.id)
    expect(data.runs.get(data.plan.id).state).toBe("NEEDS_REVIEW")
    expect(data.runs.get(data.plan.id).error).toContain("quota differs")
    expect((await data.calls()).filter((args) => args[5] === "create" && !args.includes("--dry-run"))).toHaveLength(1)
  } finally { data.store.close() }
})

test("refinement execution requires independent approval and fences concurrent SQLite owners", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const runs = new RefinementRuns(data.refinements)
  data.refinements.create(data.plan)
  expect(() => runs.ensure(data.plan.id)).toThrow("independent")
  expect(runs.list()).toEqual([])
  data.refinements.approve(data.plan.id, digest(data.plan), { directory: data.plan.directory, sessionID: data.plan.session,
    abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })
  const created = runs.ensure(data.plan.id)
  expect(runs.ensure(data.plan.id).command_digest).toBe(created.command_digest)
  const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
  const other = new RefinementRuns(new Refinements(reopened))
  try {
    expect(runs.acquire(data.plan.id, "first")).toBe(true)
    expect(other.acquire(data.plan.id, "second")).toBe(false)
    data.store.db.query("UPDATE refinement_runs SET lease_until=0 WHERE id=?").run(data.plan.id)
    expect(other.acquire(data.plan.id, "second")).toBe(true)
    expect(() => other.update(data.plan.id, "second", "RUNNING")).toThrow("Unsubmitted")
    expect(() => runs.reserve(data.plan.id, "first")).toThrow("lease lost")
    other.reserve(data.plan.id, "second")
    expect(() => other.reserve(data.plan.id, "second")).toThrow("repeat submission")
    expect(() => runs.update(data.plan.id, "first", "FAILED")).toThrow("lease lost")
    expect(other.get(data.plan.id).state).toBe("DISPATCHING")
    expect(data.store.events(data.task.id).filter((row) => (row as { kind: string }).kind === "refinement_dispatch_reserved")).toHaveLength(1)
  } finally { reopened.close(); data.store.close() }
})

for (const mode of ["success", "failed", "wrong_identity", "changed_file"] as const) {
  test(`persistent refinement reconciles real child process without resubmission: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const data = await setup(tmp.path)
    const directory = path.join(tmp.path, "remote-fixture")
    const fixture = processTransport(directory, mode, 40, true)
    const runs = approveFixture(data)
    await new RefinementRunner(runs, fixture.transport).tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("DISPATCHING")
    expect(runs.get(data.plan.id).handle).toBeNull()
    data.store.close()
    await fixture.finished()
    const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
    const resumed = new RefinementRuns(new Refinements(reopened))
    const runner = new RefinementRunner(resumed, fixture.transport)
    try {
      if (mode === "changed_file") await Bun.write(path.join(directory, "fixture-0.cif"), "changed actual bytes")
      await runner.tick(data.plan.id)
      const expected = mode === "success" ? "SUCCEEDED" : mode === "failed" ? "FAILED" : "NEEDS_REVIEW"
      expect(resumed.get(data.plan.id).state).toBe(expected)
      for (let iteration = 0; iteration < 3; iteration++) await runner.tick(data.plan.id)
      expect(resumed.get(data.plan.id).state).toBe(expected)
      expect(fixture.count()).toBe(1)
    expect(resumed.get(data.plan.id).manifest !== null).toBe(mode === "success")
      if (mode === "success") expect(resumed.get(data.plan.id).handle).toBe(resumed.get(data.plan.id).name)
      expect(resumed.list()).toHaveLength(1)
    } finally { await fixture.cleanup(); reopened.close() }
  })
}

test("refinement cancellation waits for actual child termination, not stop acknowledgement", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const fixture = processTransport(path.join(tmp.path, "remote-fixture"), "success", 10000)
  const runs = approveFixture(data)
  const runner = new RefinementRunner(runs, fixture.transport)
  try {
    await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("SUBMITTED")
    runs.cancel(data.plan.id, data.plan.directory)
    await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCEL_REQUESTED")
    await fixture.finished()
    await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCELLED")
    expect(fixture.count()).toBe(1)
    expect(runs.get(data.plan.id).manifest).toBeNull()
  } finally { await fixture.cleanup(); data.store.close() }
})

test("missing refinement observation never means stopped and never replays create", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const fixture = processTransport(path.join(tmp.path, "remote-fixture"))
  const runs = approveFixture(data)
  try {
    runs.ensure(data.plan.id)
    runs.acquire(data.plan.id, "reserved-before-lost-send")
    runs.reserve(data.plan.id, "reserved-before-lost-send")
    runs.release(data.plan.id, "reserved-before-lost-send")
    runs.cancel(data.plan.id, data.plan.directory)
    const runner = new RefinementRunner(runs, fixture.transport)
    for (let iteration = 0; iteration < 3; iteration++) await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCEL_REQUESTED")
    expect(fixture.count()).toBe(0)
  } finally { await fixture.cleanup(); data.store.close() }
})

test("an expired refinement execution is cancelled locally without starting a child", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const now = Date.now()
  data.plan = refinementPlan.parse({ ...data.plan, created: now - 1000, approve_before: now + 500, finish_before: now + 700 })
  const fixture = processTransport(path.join(tmp.path, "remote-fixture"))
  const runs = approveFixture(data)
  try {
    await Bun.sleep(Math.max(0, data.plan.finish_before - Date.now() + 10))
    await new RefinementRunner(runs, fixture.transport).tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCELLED")
    expect(runs.get(data.plan.id).submitted).toBeNull()
    expect(fixture.count()).toBe(0)
  } finally { await fixture.cleanup(); data.store.close() }
})

test("cancellation arriving during real output verification cannot become success", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const fixture = processTransport(path.join(tmp.path, "remote-fixture"))
  const runs = approveFixture(data)
  const transport = { ...fixture.transport, async verify(plan: RefinementPlan, run: ReturnType<RefinementRuns["get"]>) {
    const result = await fixture.transport.verify(plan, run)
    runs.cancel(plan.id, plan.directory)
    return result
  } }
  const runner = new RefinementRunner(runs, transport)
  try {
    await runner.tick(data.plan.id)
    await fixture.finished()
    await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCEL_REQUESTED")
    expect(runs.get(data.plan.id).manifest).toBeNull()
    await runner.tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("CANCELLED")
  } finally { await fixture.cleanup(); data.store.close() }
})

test("lease takeover during preflight prevents a stale runner from submitting", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const fixture = processTransport(path.join(tmp.path, "remote-fixture"))
  const runs = approveFixture(data)
  const transport = { ...fixture.transport, async preflight(plan: RefinementPlan, run: ReturnType<RefinementRuns["get"]>) {
    await fixture.transport.preflight(plan, run)
    data.store.db.query("UPDATE refinement_runs SET lease_until=0 WHERE id=?").run(plan.id)
    expect(runs.acquire(plan.id, "new-owner")).toBe(true)
  } }
  try {
    await new RefinementRunner(runs, transport).tick(data.plan.id)
    expect(runs.get(data.plan.id).state).toBe("WAITING")
    expect(runs.get(data.plan.id).submitted).toBeNull()
    expect(runs.get(data.plan.id).lease_owner).toBe("new-owner")
    expect(fixture.count()).toBe(0)
  } finally { await fixture.cleanup(); data.store.close() }
})

test("refinement evidence requires every identity, score, structure and failure denominator", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const result = resultFixture(data.plan)
    result.artifacts = Object.fromEntries(result.candidates.map((row) => [row.structure, "a".repeat(64)]))
    expect(verifyRefinementResult(result, data.plan).success_count).toBe(2)
    for (const value of [
      { ...result, plan_digest: "0".repeat(64) }, { ...result, code: { ...result.code, release_id: "0".repeat(64) } },
      { ...result, success_count: 1 }, { ...result, input_count: 1 }, { ...result, artifacts: {} },
      { ...result, candidates: [result.candidates[0], result.candidates[0]] },
      { ...result, candidates: result.candidates.map((row) => ({ ...row, structure: "fixture-0.cif" })) },
      { ...result, candidates: result.candidates.map((row) => ({ ...row, structure_index: 0 })) },
      { ...result, candidates: result.candidates.map((row) => ({ ...row, score: NaN })) },
      { ...result, candidates: result.candidates.map((row) => ({ ...row, structure: "../outside.cif" })) },
      { ...result, candidates: result.candidates.map((row) => ({ ...row, status: "FAILED", score: null, structure: null, failure_reason: " " })) },
      { ...result, gates: { ...result.gates, scientific_quality: false } },
    ]) expect(() => verifyRefinementResult(value, data.plan)).toThrow()
  } finally { data.store.close() }
})

test("refinement freezes a small exact list, origin, engine parameters and independent budget", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const base = data.plan
  for (const value of [
    { ...base, request: "model changed intent" }, { ...base, max_points: 0 }, { ...base, human_approved: true },
    { ...base, input: { ...base.input, candidates: [] } }, { ...base, input: { ...base.input, eligible_count: 4 } },
    { ...base, input: { ...base.input, identity_sha256: "0".repeat(64) } },
    { ...base, input: { ...base.input, candidates: [base.input.candidates[0], base.input.candidates[0]] } },
    { ...base, input: { ...base.input, candidates: base.input.candidates.map((row) => ({ ...row, rank_disagreement: 0.4 })) } },
    { ...base, engine: { ...base.engine, command: "not allowed" } },
    { ...base, engine: { ...base.engine, cpu_per_search: 11 } },
    { ...base, runtime: { ...base.runtime, python: "/inspire/../other/python" } },
  ]) expect(refinementPlan.safeParse(value).success).toBe(false)
  expect(() => data.refinements.create({ ...base, input: { ...base.input, path: "/inspire/old-pilot.parquet" } })).toThrow("frozen selection")
  data.store.state(data.task.id, "FAILED")
  expect(() => data.refinements.create(base)).toThrow("verified core")
  expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  data.store.close()
})

test("fixed refinement commands preserve exact selection, all engine choices and release paths", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const af3 = refinementCommand(data.plan, data.task)
    const option = (argv: string[], flag: string) => argv[argv.indexOf(flag) + 1]
    expect(af3.grants_resource_approval).toBe(false)
    expect(af3.runtime_verified).toBe(false)
    expect(option(af3.argv, "--candidate-table")).toBe(data.plan.input.path)
    expect(option(af3.argv, "--expected-input-count")).toBe("2")
    expect(option(af3.argv, "--expected-input-sha256")).toBe(data.plan.input.sha256)
    expect(option(af3.argv, "--expected-identity-sha256")).toBe(data.plan.input.identity_sha256)
    expect(option(af3.argv, "--top-n")).toBe("2")
    expect(af3.argv[2]).toContain(`/releases/${data.plan.code.release_id}/science/scripts/`)
    expect(af3.output).toEndWith(`/refinements/${data.plan.id}/af3`)
    expect(af3.shell).toContain("'python3' '-I' '-B' '-c'")
    expect(af3.shell).toContain("'-u' 'BASH_ENV'")
    expect(af3.shell).toContain("Staged startup plan changed")
    expect(af3.argv).not.toContain("--run-data-pipeline")
    expect(af3.argv).not.toContain("--resume")
    expect(af3.argv).not.toContain("--reuse-artifact-dir")
    if (data.plan.engine.name !== "af3") throw new Error("Fixture engine differs")
    const changed = { ...data.plan, engine: { ...data.plan.engine, num_seeds: 3, num_recycles: 5,
      random_seed: 91, cpu_per_search: 4, num_diffusion_samples: 2, run_data_pipeline: true } }
    const tuned = refinementCommand(changed, data.task)
    for (const [flag, expected] of [["--num-seeds", "3"], ["--num-recycles", "5"], ["--random-seed", "91"],
      ["--cpu-per-search", "4"], ["--num-diffusion-samples", "2"]]) expect(option(tuned.argv, flag)).toBe(expected)
    expect(tuned.argv).toContain("--run-data-pipeline")
    expect(tuned.plan_digest).not.toBe(af3.plan_digest)
    const runtime = { name: "covdock" as const, python: "/inspire/python", receptor: "/inspire/receptor.pdb",
      schrodinger_root: "/inspire/schrodinger", license_file: "/inspire/license.lic", runtime_lib_dir: "/inspire/libs", attachment_residue: "A:773" as const }
    const covdock = refinementPlan.parse({ ...data.plan, runtime,
      engine: { name: "covdock", mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.98 },
      resource: data.task.plan.steps[0].resource,
      assets: { ...data.plan.assets, ...Object.fromEntries(refinementPaths(runtime, data.task.plan.runtime.root, data.plan.code.release_id).files.map((file) => [file, "a".repeat(64)])) } })
    const command = refinementCommand(covdock, data.task)
    const evidence = resultFixture(covdock)
    evidence.candidates = evidence.candidates.map((row) => ({ ...row, structure: "combined.maegz" }))
    evidence.artifacts = { "combined.maegz": "a".repeat(64) }
    expect(verifyRefinementResult(evidence, covdock).success_count).toBe(2)
    for (const index of [null, -1, 0.5, 0]) {
      expect(() => verifyRefinementResult({ ...evidence,
        candidates: evidence.candidates.map((row) => ({ ...row, structure_index: index })) }, covdock)).toThrow()
    }
    const missing = { ...covdock.assets }
    delete missing[`${runtime.schrodinger_root}/covalent_docking`]
    expect(() => refinementCommand({ ...covdock, assets: missing }, data.task)).toThrow("frozen assets")
    for (const [flag, expected] of [["--input-score-source", "core"], ["--min-success-rate", "0.98"], ["--njobs", "1"],
      ["--host", "localhost"], ["--final-n", "2"], ["--attachment-residue", "A:773"]]) expect(option(command.argv, flag)).toBe(expected)
    const canonical = refinementPlan.parse({ ...covdock, runtime: { ...runtime, attachment_residue: "A:797" } })
    const prepared = refinementCommand(canonical, data.task)
    expect(option(prepared.argv, "--attachment-residue")).toBe("A:797")
    expect(prepared.plan_digest).not.toBe(command.plan_digest)
    expect(prepared.runtime_verified).toBe(false)
    // A different residue requires an audited target registration, not a model
    // supplied arbitrary number. Old 773 plans are not silently migrated.
    for (const attachment_residue of ["A:778", "B:797", "A:773;true"]) {
      expect(() => refinementPlan.parse({ ...covdock, runtime: { ...runtime, attachment_residue } })).toThrow()
    }
    expect(option(command.argv, "--attachment-residue")).toBe("A:773")
    expect(command.argv).not.toContain("--resume")
    expect(command.argv).not.toContain("--reuse-artifact-dir")
    expect(() => refinementCommand({ ...data.plan, assets: {} }, data.task)).toThrow()
    expect(() => refinementCommand({ ...data.plan, input: { ...data.plan.input, path: "/inspire/old.parquet" } }, data.task)).toThrow("frozen selection")
    expect(() => refinementCommand(data.plan, { ...data.task, plan: { ...data.task.plan, seed: 19 } })).toThrow("identity differs")
    const frozen = { ...data.plan.assets }
    delete frozen[data.plan.runtime.python]
    expect(() => data.refinements.create({ ...data.plan, assets: frozen })).toThrow("frozen assets")
  } finally { data.store.close() }
})

test.skipIf(!process.env.REFINEMENT_TEST_PYTHON)("generated argv reaches real Python input guards for both engines without starting compute", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const runtime = { name: "covdock" as const, python: "/inspire/python", receptor: "/inspire/receptor.pdb",
      schrodinger_root: "/inspire/schrodinger", license_file: "/inspire/license.lic", runtime_lib_dir: "/inspire/libs", attachment_residue: "A:773" as const }
    const covdock = refinementPlan.parse({ ...data.plan, runtime,
      engine: { name: "covdock", mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.95 },
      resource: data.task.plan.steps[0].resource,
      assets: { ...data.plan.assets, ...Object.fromEntries(refinementPaths(runtime, data.task.plan.runtime.root, data.plan.code.release_id).files.map((file) => [file, "a".repeat(64)])) } })
    const project = path.resolve(import.meta.dir, "../../../../../../science")
    const input = path.join(tmp.path, "guard-input.csv")
    await Bun.write(input, "input_sdf_index,compound_id,standardized_smiles\n1,one,C=CC(=O)N\n3,three,C=CC(=O)NC\n")
    const provisional = refinementPlan.parse({ ...covdock, result_contract: "screening-refinement-outcome-v2",
      quality_policy: provisionalRefinementQuality })
    for (const plan of [data.plan, covdock, provisional]) {
      const built = refinementCommand(plan, data.task)
      const output = path.join(tmp.path, plan.engine.name, "must-not-exist")
      // Rebase only filesystem endpoints into the local test fixture. Every
      // option and option value generated for engine execution remains intact,
      // apart from the deliberately wrong file hash that must stop the entry.
      const argv = built.argv.map((value, index) => {
        if (index === 0) return process.env.REFINEMENT_TEST_PYTHON!
        if (index === 2) return path.join(project, "scripts", path.basename(value))
        if (value === plan.input.path) return input
        if (value === built.output) return output
        if (built.argv[index - 1] === "--expected-input-sha256") return "0".repeat(64)
        return value
      })
      const child = Bun.spawn(argv, { cwd: tmp.path, stdin: "ignore", stdout: "pipe", stderr: "pipe",
        env: { ...process.env, PYTHONPATH: path.join(project, "src"), PYTHONDONTWRITEBYTECODE: "1" } })
      const timeout = setTimeout(() => child.kill("SIGKILL"), 15000)
      try {
        const [code, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()])
        expect(code).not.toBe(0)
        expect(stderr).toContain("Refinement candidate file differs from the frozen subset")
        expect(stderr).not.toContain("unrecognized arguments")
        expect(await Bun.file(path.join(output, "progress.json")).exists()).toBe(false)
        await expect((await import("node:fs/promises")).stat(output).catch(() => null)).resolves.toBeNull()
      } finally { clearTimeout(timeout) }
    }
  } finally { data.store.close() }
}, 30000)

test("wrong conversation or failed fresh verification cannot create a refinement card", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const calls: string[] = []
  const context = { sessionID: data.plan.session, directory: tmp.path, abort: new AbortController().signal,
    metadata: () => { calls.push("metadata") }, ask: async () => { calls.push("ask") } }
  await expect(proposeRefinement(data.refinements, data.plan, { ...context, sessionID: "different-session" },
    async () => { calls.push("verify") })).rejects.toThrow("another conversation")
  expect(calls).toEqual([])
  await expect(proposeRefinement(data.refinements, data.plan, context,
    async () => { throw new Error("Fresh engine preflight failed") })).rejects.toThrow("preflight failed")
  expect(calls).toEqual([])
  expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  data.store.close()
})

test("engine interface and guard must be supported and frozen, not merely present on disk", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const root = `${data.task.plan.runtime.root}/releases/${data.plan.code.release_id}/science`
  const entry = `${root}/scripts/run_af3_registered_batch.py`
  const helper = `${root}/src/covscreen/refinement_input.py`
  const plan = { ...data.plan, assets: { ...data.plan.assets, [entry]: "1".repeat(64), [helper]: "2".repeat(64) } }
  const proof = { engine: "af3", entry, helper, entry_sha256: "1".repeat(64), helper_sha256: "2".repeat(64),
    passes_static_checks: true, exact_input_guard_present: true, missing_options: [], blocking_reasons: [],
    runtime_verified: false, license_checkout_verified: false, resource_approval_granted: false, code: data.plan.code }
  expect(verifyEngineInterface(proof, plan, data.task).runtime_verified).toBe(false)
  const measured = refinementPlan.parse({ ...plan, geometry_contract: "af3-geometry-evidence-v1" })
  expect(() => verifyEngineInterface(proof, measured, data.task)).toThrow("geometry readback support")
  expect(verifyEngineInterface({ ...proof, geometry_contracts: ["af3-geometry-evidence-v1"] }, measured, data.task).runtime_verified).toBe(false)
  for (const change of [{ engine: "covdock" }, { entry: "/inspire/old-pilot.py" },
    { passes_static_checks: false }, { exact_input_guard_present: false }, { missing_options: ["--expected-input-count"] },
    { helper_sha256: null }, { entry_sha256: "0".repeat(64) }, { blocking_reasons: ["missing contract"] },
    { resource_approval_granted: true }, { runtime_verified: true }]) {
    expect(() => verifyEngineInterface({ ...proof, ...change }, plan, data.task)).toThrow()
  }
  expect(() => verifyEngineInterface({ ...proof, code: { ...data.plan.code, release_id: "0".repeat(64) } }, plan, data.task)).toThrow("code release")
  expect(() => verifyEngineInterface(proof, data.plan, data.task)).toThrow("frozen assets")
  data.store.close()
})

test("classified CovDock cannot use a release that only trusts saved structure audits", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const plan = workflowFixture(data).stages[1]
  const root = `${data.task.plan.runtime.root}/releases/${plan.code.release_id}/science`
  const entry = `${root}/scripts/run_covdock_lo_registered.py`
  const helper = `${root}/src/covscreen/refinement_input.py`
  const proof = { engine: "covdock", entry, helper, entry_sha256: plan.assets[entry], helper_sha256: plan.assets[helper],
    passes_static_checks: true, exact_input_guard_present: true, missing_options: [], blocking_reasons: [],
    input_score_sources: ["core"], runtime_verified: false, license_checkout_verified: false,
    resource_approval_granted: false, code: plan.code }
  expect(() => verifyEngineInterface(proof, plan, data.task)).toThrow("original structure SDK readback")
  expect(() => verifyEngineInterface({ ...proof, original_structure_readback_supported: false }, plan, data.task)).toThrow("original structure SDK readback")
  expect(verifyEngineInterface({ ...proof, original_structure_readback_supported: true }, plan, data.task).runtime_verified).toBe(false)
  expect(refinementPaths(plan.runtime, data.task.plan.runtime.root, plan.code.release_id).files).toContain(`${root}/scripts/inspect_covdock_output.py`)
  data.store.close()
})

test("fresh selection proof must match every frozen identity, reason, input and report", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const plan = data.plan
  const proof = { selection_verified: true, selection_id: plan.selection_id, source_task_id: plan.parent_id,
    source_plan_digest: plan.parent_digest, source_manifest_digest: plan.report_digest,
    selection_manifest_digest: plan.selection_manifest_digest, status: "SUBSET_PROPOSED", input_count: 8,
    eligible_count: 8, excluded_count: 0, selected_count: 2, effective_cap: 2,
    artifact_directory: plan.input.path.slice(0, -"/candidates.parquet".length), input: plan.input,
    calibrated_uncertainty: false, gpu_submitted: false, refinement_approval_required: true }
  expect(verifySelectionEvidence(proof, plan).input.candidates).toEqual(plan.input.candidates)
  for (const changed of [
    { selection_verified: false }, { source_task_id: "different" }, { source_plan_digest: "0".repeat(64) },
    { source_manifest_digest: "0".repeat(64) }, { selection_manifest_digest: "0".repeat(64) },
    { selected_count: 1 }, { effective_cap: 3 }, { eligible_count: 9 }, { input_count: 9 },
    { artifact_directory: "/inspire/old-pilot" }, { gpu_submitted: true },
    { input: { ...plan.input, sha256: "0".repeat(64) } },
    { input: { ...plan.input, candidates: plan.input.candidates.map((row) => ({ ...row, selection_channel: "diversity_sentinel" })) } },
  ]) expect(() => verifySelectionEvidence({ ...proof, ...changed }, plan)).toThrow()
  // A real Inspire transport is present, but incomplete core state must reject
  // before it can run its CLI, rather than trusting a previously saved proof.
  data.store.state(data.task.id, "FAILED")
  await expect(verifyRefinementSelection(data.store, plan, new Inspire(data.task.plan.connection)))
    .rejects.toThrow("source report not currently verified")
  expect(data.store.get(data.task.id).state).toBe("FAILED")
  // Even an apparently completed saved core cannot open approval if the actual
  // CLI cannot re-read its files. No replacement verifier or model boolean.
  data.store.state(data.task.id, "SUCCEEDED")
  const calls: string[] = []
  await expect(proposeVerifiedRefinement(data.refinements, plan, {
    sessionID: plan.session, directory: tmp.path, abort: new AbortController().signal,
    metadata: () => { calls.push("metadata") }, ask: async () => { calls.push("ask") },
  }, new Inspire(data.task.plan.connection), async () => { calls.push("preflight") }))
    .rejects.toThrow("source report not currently verified")
  expect(calls).toEqual([])
  expect(data.store.db.query("SELECT * FROM refinements").all()).toEqual([])
  data.store.close()
})

for (const classified of [false, true]) for (const decision of ["once", "reject", "abort", "cancel", "expire", "tamper", "changed_file", "parent_changed"] as const) {
  test(`independent native refinement approval (${classified ? "classified" : "legacy"}): ${decision}`, async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const data = await setup(tmp.path, "/never-invoked", "af3", classified)
      const plan = { ...data.plan, ...(decision === "expire" ? { approve_before: Date.now() + 300 } : {}) }
      const coreReceipt = data.store.get(data.task.id).approval
      const controller = new AbortController()
      const requested = Promise.withResolvers<PermissionNext.Request>()
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID === plan.session) requested.resolve(event.properties)
      })
      const checks: string[] = []
      const metadata: Record<string, unknown>[] = []
      const pending = proposeRefinement(data.refinements, plan, { sessionID: plan.session, directory: tmp.path,
        abort: controller.signal, metadata: (value) => { metadata.push(value.metadata ?? {}) },
        ask: (input) => PermissionNext.ask({ ...input, sessionID: plan.session, signal: controller.signal,
          ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }),
      }, async (value) => {
        checks.push(value.id)
        if (await data.checksum() !== value.input.sha256) throw new Error("Actual local subset bytes changed")
      }).then((task) => ({ task, error: undefined }), (error: unknown) => ({ task: undefined, error }))
      try {
        const card = await requested.promise
        expect(card.patterns).toEqual([digest(plan)])
        expect(card.patterns).not.toContain(data.task.digest)
        expect(card.metadata.compute.selected_candidates).toEqual(plan.input.candidates)
        expect(card.metadata.compute.engine_parameters).toEqual(plan.engine)
        expect(card.metadata.compute.result_contract).toBe(classified ? "screening-refinement-outcome-v2" : "screening-refinement-outcome-v1")
        expect(card.metadata.compute.runtime_assets).toEqual(plan.runtime)
        expect(card.metadata.compute.maximum_compute_points).toBe(0.2)
        const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
        expect(new Refinements(reopened).get(plan.id).approval).toBeNull()
        reopened.close()
        expect(data.refinements.get(plan.id).state).toBe("DRAFT")
        if (decision === "abort") controller.abort()
        if (decision === "cancel") data.refinements.cancel(plan.id, tmp.path)
        if (decision === "expire") await Bun.sleep(Math.max(0, plan.approve_before - Date.now() + 10))
        if (decision === "tamper") data.store.db.query("UPDATE refinements SET plan=? WHERE id=?")
          .run(JSON.stringify({ ...plan, ...(classified ? { result_contract: undefined } : { minutes: 9 }) }), plan.id)
        if (decision === "changed_file") await Bun.write(data.file, "changed actual subset bytes")
        if (decision === "parent_changed") data.store.state(data.task.id, "NEEDS_REVIEW")
        if (decision !== "abort")
          await PermissionNext.reply({ requestID: card.id, reply: decision === "reject" ? "reject" : "once" })
        const result = await pending
        if (decision === "once") {
          expect(result.error).toBeUndefined()
          expect(result.task?.state).toBe("APPROVED")
          expect(JSON.parse(result.task!.approval!).digest).toBe(digest(plan))
          expect(checks).toHaveLength(2)
        } else {
          expect(result.error).toBeDefined()
          expect(data.store.db.query("SELECT approval FROM refinements WHERE id=?").get(plan.id)).toEqual({ approval: null })
          if (decision !== "tamper") {
            expect(data.refinements.get(plan.id).state).toBe("CANCELLED")
            expect(metadata.at(-1)?.state).toBe("CANCELLED")
            // The result is durable, not merely the disappearance of a card.
            const reopened = new Store(path.join(tmp.path, "refinement.sqlite"))
            try {
              const refinements = new Refinements(reopened)
              expect(refinements.get(plan.id).state).toBe("CANCELLED")
              expect(() => new RefinementRuns(refinements).ensure(plan.id)).toThrow("independent")
              expect(() => refinements.approve(plan.id, digest(plan), { directory: tmp.path, sessionID: plan.session,
                abort: new AbortController().signal, metadata: () => {}, ask: async () => {} })).toThrow("identity/state")
            } finally { reopened.close() }
          }
          if (decision === "tamper") {
            expect(metadata.at(-1)?.state).toBe("DRAFT")
            expect(data.store.events(plan.parent_id).some((row) =>
              (row as { kind: string }).kind === "refinement_approval_cleanup_requires_review")).toBe(true)
          }
        }
        if (decision === "abort") {
          expect((await PermissionNext.list()).some((item) => item.id === card.id)).toBe(false)
          await PermissionNext.reply({ requestID: card.id, reply: "once" })
          expect(data.refinements.get(plan.id).state).toBe("CANCELLED")
          expect(data.refinements.get(plan.id).approval).toBeNull()
        }
        expect(data.store.get(data.task.id).approval).toBe(coreReceipt)
        expect(data.store.stages(data.task.id).every((stage) => stage.submitted === null)).toBe(true)
        expect(data.store.db.query("SELECT * FROM daemon").all()).toEqual([])
      } finally { unsubscribe(); data.store.close() }
    } })
  })
}

test("refinement approval cleanup is exact-context, idempotent and cannot revoke an approved execution", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const context = { directory: tmp.path, sessionID: data.plan.session,
    abort: new AbortController().signal, metadata: () => {}, ask: async () => {} }
  try {
    const task = data.refinements.create(data.plan)
    for (const [expected, scope] of [["0".repeat(64), context], [task.digest, { ...context, sessionID: "other" }],
      [task.digest, { ...context, directory: "/another-project" }]] as const) {
      expect(() => data.refinements.endApproval(task.id, expected, scope, "fixture interruption")).toThrow("another")
      expect(data.refinements.get(task.id).state).toBe("DRAFT")
    }
    data.refinements.approve(task.id, task.digest, context)
    const runs = new RefinementRuns(data.refinements)
    runs.ensure(task.id)
    const approved = data.refinements.get(task.id)
    expect(data.refinements.endApproval(task.id, task.digest, context, "late interruption")).toEqual(approved)
    expect(runs.get(task.id).state).toBe("WAITING")
    // A malformed unapproved row with an execution must not be disguised as a
    // clean cancellation; preserve the evidence for review, never dispatch it.
    data.store.db.query("UPDATE refinements SET state='DRAFT',approval=NULL WHERE id=?").run(task.id)
    expect(() => data.refinements.endApproval(task.id, task.digest, context, "interruption")).toThrow("execution")
    expect(data.refinements.get(task.id).state).toBe("DRAFT")
    expect(runs.get(task.id).submitted).toBeNull()
  } finally { data.store.close() }
})

test("refinement closes a draft if native approval presentation fails and preserves the original error", async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  const failure = new Error("Native presentation unavailable")
  const calls: string[] = []
  try {
    const result = await proposeRefinement(data.refinements, data.plan, {
      directory: tmp.path, sessionID: data.plan.session, abort: new AbortController().signal,
      metadata: () => { calls.push("metadata"); throw failure },
      ask: async () => { calls.push("ask") },
    }, async () => { calls.push("verified") }).catch((error: unknown) => error)
    expect(result).toBe(failure)
    expect(calls).toEqual(["verified", "metadata", "metadata"])
    const task = data.refinements.get(data.plan.id)
    expect(task.state).toBe("CANCELLED")
    expect(task.approval).toBeNull()
    expect(data.refinements.endApproval(task.id, task.digest,
      { directory: tmp.path, sessionID: data.plan.session }, "repeated cleanup")).toEqual(task)
    expect(data.store.events(data.plan.parent_id).filter((row) =>
      (row as { kind: string }).kind === "refinement_approval_ended_without_grant")).toHaveLength(1)
    expect(() => new RefinementRuns(data.refinements).ensure(task.id)).toThrow("independent")
  } finally { data.store.close() }
})

for (const name of ["unbound", "bound"]) test("pre-migration " + name + " proposal replays byte-identical policy and digest", async () => {
  await using tmp = await tmpdir()
  const root = path.join(import.meta.dir, "fixtures/proposal-migration", name)
  const fixture = await Bun.file(root + ".json").json()
  const original = await Bun.file(root + ".sqlite").arrayBuffer()
  const file = path.join(tmp.path, "legacy.sqlite")
  await Bun.write(file, original)
  const store = new Store(file)
  try {
    const task = store.get(fixture.task_id)
    const saved = refinementProposalSnapshot(store, task, fixture.scope)
    expect(saved?.state).toBe("AVAILABLE")
    expect(saved?.proposal).toEqual(fixture.proposal)
    expect(digest(saved?.proposal)).toBe(fixture.digest)
    expect(saved?.proposal?.quality_policy.policy).toEqual(defaultRefinementQuality)
    const fresh = refinementProposal(store, task, fixture.proposal.selection, {
      af3: fixture.proposal.stages[0].resource, covdock: fixture.proposal.stages[1].resource,
      recorded_at: fixture.proposal.resource_quote_recorded_at,
    }, fixture.proposal.created_at, fixture.scope)
    expect(fresh.quality_policy.policy).toEqual(provisionalRefinementQuality)
    expect(refinementProposalSnapshot(store, task, fixture.scope)?.proposal).toEqual(fixture.proposal)
    for (const [outer, inner] of [
      ["unknown-version", "unknown-version"],
      [fixture.proposal.version, fresh.version],
      [name === "bound" ? refinementProposalVersion : boundRefinementProposalVersion, fresh.version],
    ]) {
      const value = { ...fixture.proposal, version: inner }
      store.event(task.id, "refinement_execution_proposal", { state: "AVAILABLE", version: outer,
        value, digest: digest(value), ...(fixture.scope ? { planning_scope: fixture.scope } : {}) })
      expect(refinementProposalSnapshot(store, task, fixture.scope)?.state).toBe("UNAVAILABLE")
    }
    // Rehashing a legacy policy under the new version cannot downgrade a new plan.
    const downgraded = { ...fresh, quality_policy: fixture.proposal.quality_policy }
    store.event(task.id, "refinement_execution_proposal", { state: "AVAILABLE", version: fresh.version,
      value: downgraded, digest: digest(downgraded), ...(fixture.scope ? { planning_scope: fixture.scope } : {}) })
    expect(refinementProposalSnapshot(store, task, fixture.scope)?.state).toBe("UNAVAILABLE")
    expect(store.db.query("SELECT * FROM refinements").all()).toEqual([])
  } finally { store.close() }
  expect(await Bun.file(root + ".sqlite").arrayBuffer()).toEqual(original)
})

for (const strict of [false, true]) for (const mode of ["retry", "recovery"] as const)
test("proposal migration preserves frozen " + (strict ? "v2" : "v1") + " policy during " + mode, async () => {
  await using tmp = await tmpdir({ git: true })
  const data = await setup(tmp.path)
  try {
    const policy = strict ? provisionalRefinementQuality : defaultRefinementQuality
    const task = data.store.get(data.task.id)
    const fixture = diagnosticFixture(data)
    const prefix = task.plan.runtime.root + "/releases/" + data.plan.code.release_id + "/"
    const names = [fixture.profile.af3, fixture.profile.covdock].flatMap(runtime =>
      refinementPaths(runtime, task.plan.runtime.root, data.plan.code.release_id).files)
      .filter(file => file.startsWith(prefix)).map(file => file.slice(prefix.length))
    const inventory = { version: "screening-code-release-v1", files: Object.fromEntries(names.map(name => [name, "a".repeat(64)])) }
    const code = { release_id: digest(inventory), manifest_sha256: new Bun.CryptoHasher("sha256")
      .update(JSON.stringify(canonical(inventory)) + "\n").digest("hex"), checker_sha256: "a".repeat(64) }
    const profile = refinementProfile.parse({ ...fixture.profile, code })
    await diagnoseRefinement(data.store, task.id, new AbortController().signal, async () => profile,
      async (_task, _profile, nonce) => ({ ...fixture.value, code, profile_digest: digest(profile), nonce }))
    const now = Date.now()
    const subset = { ...proposalFixture(data), policy: refinementSelectionPolicy }
    const rates = { af3: task.plan.steps[1].resource, covdock: task.plan.steps[0].resource, recorded_at: now }
    const initial = refinementProposal(data.store, task, subset, rates, now)
    const save = (value: ReturnType<typeof refinementProposal>) => data.store.event(task.id, "refinement_execution_proposal",
      { state: "AVAILABLE", version: value.version, value, digest: digest(value) })
    save(initial)
    const source = { text: data.plan.request, origin: data.plan.origin }
    const built = buildRefinementWorkflow(data.store, task.id, digest(initial), source, profile, inventory, now)
    const prior = refinementWorkflow.parse({ ...built.workflow,
      stages: built.workflow.stages.map(stage => ({ ...stage, quality_policy: policy })) })
    const workflows = new RefinementWorkflows(data.refinements)
    workflows.create(prior)
    workflows.approve(prior.id, digest(prior), { directory: tmp.path, sessionID: prior.stages[0].session,
      abort: new AbortController().signal, metadata() {}, async ask() {} })
    const runs = new RefinementRuns(data.refinements)
    for (const stage of prior.stages) { runs.ensure(stage.id); runs.acquire(stage.id, "fixture") }
    const reserved = runs.reserve(prior.stages[0].id, "fixture")
    if (mode === "retry") {
      runs.update(prior.stages[0].id, "fixture", "FAILED", "Fixture failure", reserved.name)
      runs.cancel(prior.stages[1].id, tmp.path)
      runs.update(prior.stages[1].id, "fixture", "CANCELLED")
    } else {
      runs.update(prior.stages[0].id, "fixture", "VERIFYING", null, reserved.name)
      const quality = qualityFixture({ ...data, plan: prior.stages[0] }, "af3", strict)
      runs.complete(prior.stages[0].id, "fixture", evaluateRefinementQuality(prior.stages[0], task, quality.receipt))
      const covdock = runs.reserve(prior.stages[1].id, "fixture")
      runs.update(prior.stages[1].id, "fixture", "FAILED", "Fixture failure", covdock.name)
    }
    for (const stage of prior.stages) runs.release(stage.id, "fixture")
    const before = digest({ prior: workflows.get(prior.id), runs: runs.list() })
    // A proposal prepared before the failure cannot silently become a retry.
    expect(() => buildRefinementWorkflow(data.store, task.id, digest(initial), source, profile, inventory, now + 1))
      .toThrow("continuation changed")
    const value = refinementProposal(data.store, task, subset, rates, now + 1)
    expect(value.continuation).toMatchObject({ kind: mode, id: prior.id, digest: digest(prior) })
    expect(value.quality_policy.policy).toEqual(policy)
    save(value)
    expect(refinementProposalSnapshot(data.store, task)?.proposal).toEqual(value)
    const next = buildRefinementWorkflow(data.store, task.id, digest(value), source, profile, inventory, now + 2)
    expect(next.workflow.stages.every(stage => digest(stage.quality_policy) === digest(policy))).toBe(true)
    if (mode === "recovery") {
      expect(next.workflow.stages[0]).toEqual(prior.stages[0])
      expect(next.workflow.max_points).toBe(next.workflow.stages[1].max_points)
    } else expect(next.workflow.retry_of?.id).toBe(prior.id)
    workflows.create(next.workflow)
    // Reading the saved proposal uses its bound ancestor, not the latest draft.
    expect(refinementProposalSnapshot(data.store, task)?.proposal).toEqual(value)
    data.store.event(task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...next })
    expect(refinementDraftSnapshot(data.store, task.id, now + 2)?.state).toBe("COMPILED_NOT_EXECUTABLE")
    expect(digest({ prior: workflows.get(prior.id), runs: runs.list() })).toBe(before)
    expect(workflows.get(next.workflow.id).approval).toBeNull()
    const view = presentRefinement(refinementProposalSnapshot(data.store, task))
    if (mode === "recovery") expect(view && "interpretation" in view && view.interpretation.approval).toContain("不重复提交 AF3")
    if (mode === "retry") {
      const detached = { ...next.workflow, retry_of: undefined }
      data.store.event(task.id, "refinement_workflow_draft", { state: "COMPILED_NOT_EXECUTABLE", ...next,
        workflow: detached, digest: digest(detached) })
      expect(refinementDraftSnapshot(data.store, task.id, now + 2)?.state).toBe("UNAVAILABLE")
    }
    const tampered = { ...value, continuation: { ...value.continuation!, digest: "0".repeat(64) } }
    save(tampered)
    expect(refinementProposalSnapshot(data.store, task)?.state).toBe("UNAVAILABLE")
  } finally { data.store.close() }
})
