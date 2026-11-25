import { expect, test } from "bun:test"
import path from "node:path"
import { chmod, realpath } from "node:fs/promises"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { dynamicTool, jsonSchema, type Tool } from "ai"
import { tmpdir } from "../../../test/fixture/fixture"
import { libraryWorkflowTools, libraryStorage } from "../../../src/session/library-workflow"
import { Storage } from "../../../src/storage/storage"
import { Store } from "./store"
import { fixture, refinementFixture } from "./workflow-plan.fixture"
import { prepareWorkflowScreening } from "./workflow-plan"
import { workflowLedgerIdentity, runWorkflowScreening } from "./workflow-host"
import { workflowObservation } from "./workflow-execution"
import { digest } from "./model"
import { screeningResult } from "./workflow-results"
import { refinementPlanningScope, retainPlanningEvent } from "./refinement-planning-scope"
import { verifySelectionProposal } from "./selection-proposal"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"

const workspace = path.resolve(import.meta.dir, "../../../../../..")
const python = path.join(workspace, ".cache/refinement-validation-python310/bin/python")

test("real MCP result review binds an exact subset, survives reply loss, preserves zero candidates and blocks stale files", async () => {
  await using tmp = await tmpdir({ git: true })
  const root = await realpath(tmp.path)
  const f = fixture(root)
  const scope = { project: "fixture", ...f.scope }
  const key = path.join(root, "test-only.key")
  await Bun.write(key, "isolated-workflow-results-fixture-key-123456789")
  await chmod(key, 0o600)
  const prior = process.env.COVALENT_APPROVAL_KEY_FILE
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  const cli = path.join(root, "selection.ts")
  await Bun.write(cli, `#!${process.execPath}\n` + await Bun.file(path.join(import.meta.dir, "selection-cli.fixture.ts")).text())
  await chmod(cli, 0o700)
  f.environment.connection.cli = cli
  const store = new Store(path.join(root, "tasks.sqlite"))
  const client = new Client({ name: "workflow-result-fixture", version: "1" })
  try {
    const saved = prepareWorkflowScreening(store, f.intent, f.scope, f.environment)
    // Explicit software fixture: populate a completed native ledger without
    // dispatching an engine or starting an observer. Fresh file checks below
    // use the real transport against a local CLI contract simulator.
    store.approve(saved.task.id, saved.task.digest, f.scope.sessionID, root)
    const artifact = "Software-only result evidence"
    await Bun.write(cli + ".artifact", artifact)
    const manifests: Record<string, unknown>[] = []
    for (const stage of store.stages(saved.task.id)) {
      const manifest = { task_id: saved.task.id, phase: stage.phase, plan_digest: saved.task.digest, status: "SUCCEEDED",
        parent_digest: manifests.length ? digest(manifests.at(-1)) : null, input_count: 5, success_count: 5, failed_count: 0,
        artifacts: { "fixture.txt": new Bun.CryptoHasher("sha256").update(artifact).digest("hex") }, identity_digest: "d".repeat(64),
        gates: { fixture: true }, runtime_seconds: 1 }
      manifests.push(manifest)
      store.stage(saved.task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest) })
      await Bun.write(cli + "." + stage.phase + ".json", JSON.stringify(manifest))
    }
    store.state(saved.task.id, "SUCCEEDED")
    const approval = store.get(saved.task.id).approval
    const policy = { max_candidates: 1, max_fraction: 0.25, disagreement_threshold: 0.5, top_fraction: 0.2,
      uncertainty_count: 1, consensus_count: 0, diversity_count: 0 }
    const candidates = [{ input_sdf_index: 2, compound_id: "fixture-two", selection_channel: "rank_disagreement_review",
      carsidock_rank_fraction: 0, rtmscore_rank_fraction: 1, rank_disagreement: 1 }]
    const directory = `${saved.task.plan.runtime.root}/tasks/${saved.task.id}/selections/${"e".repeat(64)}`
    const proof = { selection_verified: true, selection_id: "e".repeat(64), source_task_id: saved.task.id,
      source_plan_digest: saved.task.digest, source_manifest_digest: digest(manifests[3]), selection_manifest_digest: "f".repeat(64),
      status: "SUBSET_PROPOSED", policy, input_count: 5, eligible_count: 5, excluded_count: 0, selected_count: 1, effective_cap: 1,
      artifact_directory: directory, input: { path: directory + "/candidates.parquet", sha256: "a".repeat(64),
        identity_sha256: digest([[2, "fixture-two"]]), eligible_count: 5, candidates, calibrated_uncertainty: false },
      calibrated_uncertainty: false, gpu_submitted: false, refinement_approval_required: true,
      limitations: ["Software fixture; rank disagreement is not calibrated uncertainty or activity"] }
    const metadata = { task: saved.task.id, digest: saved.task.digest, proposal: proof }
    await Bun.write(cli + ".json", JSON.stringify(metadata))
    const observation = workflowObservation(store, saved.reference, f.scope)
    const handoff = { scope: { ...scope, ledger_id: workflowLedgerIdentity(store) }, source: f.environment.source,
      intent: f.intent, screening_plan: { id: "screening-plan-" + "a".repeat(32), sha256: "a".repeat(64) },
      receipt: { task_id: saved.task.id, reference: saved.reference, state: "DRAFT", preflight_verified: true }, observation }
    await Bun.write(path.join(root, "fixture.json"), JSON.stringify(handoff))
    const bootstrap = `import json,sys
from pathlib import Path
from covscreen.library_workflow import navigate
from covscreen_library_first.workflow import directory
from covscreen.workflow_store import load,save
from covscreen.mcp.server import main
root=Path(sys.argv[1]); h=json.loads((root/'fixture.json').read_text())
start=navigate(root,action='start',request_id='software-completed-ledger',target='EGFR',taxon=9606)
old=directory(root,start['workflow']['workflow_id']); state=load(old)
state.update(workflow_id=h['intent']['workflow_id'],revision=10,stage='SCREENING_REVIEW',screening_plan=h['screening_plan'],screening_handoff=h)
folder=directory(root,state['workflow_id']); folder.mkdir(); save(folder,state)
main(root)`
    await client.connect(new StdioClientTransport({ command: python, args: ["-B", "-c", bootstrap, root],
      env: { ...process.env, PYTHONPATH: path.join(workspace, "science/src"), COVALENT_APPROVAL_KEY_FILE: key }, stderr: "pipe" }))
    const definitions = await client.listTools()
    const tools: Record<string, Tool> = Object.fromEntries(definitions.tools.map(tool => ["science_" + tool.name,
      dynamicTool({ inputSchema: jsonSchema(tool.inputSchema), execute: args => client.callTool({ name: tool.name,
        arguments: args as Record<string, unknown> }) })]))
    await Storage.write([libraryStorage, scope.project, scope.sessionID], { entry: "science_covalent_library_workflow",
      workflow_id: f.intent.workflow_id, active: true })
    const flags = { lose: true, loseRefinement: true, loseRelease: true, observerFailure: false }
    const observerStarts: number[] = []
    const nativeContext = { sessionID: scope.sessionID, directory: scope.directory, abort: new AbortController().signal,
      metadata() {}, async ask() { throw Error("Recovery fixture must never ask for another approval") } }
    const projection = { project: scope.project, session: scope.sessionID, intent: true,
      screening: (args: Record<string, unknown>, _options: unknown, execute: (args: Record<string, unknown>) => Promise<unknown>) =>
        runWorkflowScreening({ store, scope, args, source: args.action === "plan_refinement" ? f.environment.source : undefined,
          native: { context: nativeContext, observer: async () => {
            observerStarts.push(Date.now())
            if (flags.observerFailure) { flags.observerFailure = false; throw Error("Fixture observer startup failed after saved grant") }
          } },
          signal: new AbortController().signal, execute: async values => {
          const request = JSON.parse(Buffer.from(String(values._workflow_handoff).split(".")[0], "base64url").toString())
          if (request.operation === "refinement_release" && flags.loseRelease) {
            flags.loseRelease = false; throw Error("Fixture lost backend connection after native planning closure")
          }
          const result = await execute(values)
          if (args.action === "plan_refinement" && request.operation === "open") {
            // Persist an unavailable native proposal as a software fixture.
            // The actual adapter must recover it without environment access,
            // reselection or replacement. No remote planner is simulated.
            const state = await Bun.file(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json")).json()
            const planning = refinementPlanningScope.parse(state.refinement_handoff.planning_scope)
            retainPlanningEvent(store, saved.task.id, "refinement_execution_proposal", { state: "UNAVAILABLE" }, planning)
          }
          if (request.operation === "refinement_plan" && flags.loseRefinement) {
            flags.loseRefinement = false; throw Error("Fixture lost refinement reply after commit")
          }
          if (args.action === "select_screening_candidates" && request.operation === "result" && flags.lose) {
            flags.lose = false; throw Error("Fixture lost result reply after commit")
          }
          return result
        } }) }
    const invoke = async (args: Record<string, unknown>) => {
      const current = await libraryWorkflowTools(tools, projection)
      const value = await current.science_covalent_library_research.execute!(args, { toolCallId: "result-review", messages: [] })
      const parsed = value as { structuredContent: { workflow: { stage: string; tools: Record<string, { actions: string[] }> }; screening_result: unknown; assessment: { state: string } } }
      expect(value).not.toHaveProperty("isError", true)
      expect(JSON.stringify(value)).not.toContain("/inspire/")
      expect(JSON.stringify(value)).not.toContain("_host_handoff")
      expect(parsed.structuredContent.assessment.state).toBe("not_evaluated")
      return parsed.structuredContent
    }
    const report = await invoke({ action: "screening_report" })
    expect(report.screening_result).toMatchObject({ verification: "verified", selection: null, stages: [{ input_count: 5 }, {}, {}, {}] })
    await expect(invoke({ action: "select_screening_candidates", selection_policy: policy })).rejects.toThrow()
    await expect(invoke({ action: "select_screening_candidates", selection_policy: policy,
      reason: "Review one disagreement candidate with the original denominator" })).rejects.toThrow("lost result reply")
    const read = await invoke({ action: "read_screening_candidates" })
    expect(read.workflow.stage).toBe("CANDIDATE_REVIEW")
    expect(read.screening_result).toMatchObject({ verification: "verified", selection: { candidates, selected_count: 1, input_count: 5 } })
    expect((await invoke({ action: "screening_status" })).workflow.stage).toBe("CANDIDATE_REVIEW")
    // A changed latest native proposal cannot silently replace the bound one.
    await Bun.write(cli + ".json", JSON.stringify({ ...metadata, proposal: { ...proof, selection_id: "9".repeat(64) } }))
    const missing = await invoke({ action: "read_screening_candidates" })
    expect(missing.workflow.stage).toBe("SCREENING_REVIEW")
    expect(missing.screening_result).toMatchObject({ verification: "selection_unavailable", selection: null })
    expect(missing.workflow.tools.covalent_library_research.actions).toContain("select_screening_candidates")
    await Bun.write(cli + ".json", JSON.stringify(metadata))
    expect((await invoke({ action: "screening_report" })).workflow.stage).toBe("CANDIDATE_REVIEW")
    await Bun.write(cli + ".artifact", "tampered")
    const bad = await invoke({ action: "screening_report" })
    expect(bad.screening_result).toMatchObject({ verification: "verification_unavailable", selection: null, stages: [] })
    expect(bad.workflow.stage).toBe("SCREENING_REVIEW")
    expect(bad.workflow.tools.covalent_library_research.actions).not.toContain("select_screening_candidates")
    await Bun.write(cli + ".artifact", artifact)
    await invoke({ action: "screening_report" })
    const emptyPolicy = { ...policy, disagreement_threshold: 0.99 }
    const emptyDirectory = directory.replace("e".repeat(64), "9".repeat(64))
    await Bun.write(cli + ".json", JSON.stringify({ ...metadata, proposal: { ...proof, selection_id: "9".repeat(64),
      policy: emptyPolicy, status: "NO_CANDIDATES_MEET_POLICY", selected_count: 0, artifact_directory: emptyDirectory,
      input: { ...proof.input, path: emptyDirectory + "/candidates.parquet", candidates: [], identity_sha256: digest([]) } } }))
    const empty = await invoke({ action: "select_screening_candidates", selection_policy: emptyPolicy,
      reason: "Test an explicit stricter selection policy without filling any unused quotas" })
    expect(empty.workflow.stage).toBe("SCREENING_SELECTION_EMPTY")
    expect(empty.screening_result).toMatchObject({ selection: { selected_count: 0, candidates: [], input_count: 5, eligible_count: 5 } })
    expect((await invoke({ action: "screening_status" })).workflow.stage).toBe("SCREENING_SELECTION_EMPTY")
    expect(store.list()).toHaveLength(1)
    expect(store.get(saved.task.id).approval).toBe(approval)
    const calls = (await Bun.file(cli + ".calls").text()).trim().split("\n").map(line => JSON.parse(line) as string[])
    expect(calls.every(args => args[4] === "notebook" && args[5] === "exec")).toBe(true)
    const state = await Bun.file(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json")).json()
    expect(state.screening_handoff.selection_history).toHaveLength(1)
    expect(state.screening_handoff.selection_history[0].selected_count).toBe(1)
    expect(screeningResult.parse(state.screening_handoff.screening_result).selection?.selected_count).toBe(0)
    expect(empty.workflow.tools.covalent_library_research.actions).not.toContain("plan_refinement")
    await Bun.write(cli + ".json", JSON.stringify(metadata))
    const reviewed = await invoke({ action: "select_screening_candidates", selection_policy: policy,
      reason: "Restore an explicitly reviewed nonempty subset for software planning recovery" })
    expect(reviewed.workflow.tools.covalent_library_research.actions).toContain("plan_refinement")
    const beforePlanning = await Bun.file(cli + ".calls").text()
    await expect(invoke({ action: "plan_refinement", max_points: 1,
      refinement_review: "Review the bound subset under an independent software-only budget" })).rejects.toThrow("lost refinement reply")
    const locked = await Bun.file(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json")).json()
    expect(locked.stage).toBe("REFINEMENT_PLAN_BLOCKED")
    expect(locked.refinement_handoff.planning_scope.selection.selection_id).toBe(proof.selection_id)
    const restored = await invoke({ action: "recover_refinement" })
    expect(restored.workflow.stage).toBe("REFINEMENT_PLAN_BLOCKED")
    expect(restored).toMatchObject({ execution: { scope: "refinement_draft_preparation", state: "not_submitted", task_ids: [] },
      refinement_plan: { operation_id: locked.refinement_handoff.planning_scope.operation_id,
        proposal: null, draft: null, approval_recorded: false, compute_submitted: false } })
    expect(restored.workflow.tools.covalent_library_research.actions).toEqual(["read_screening_plan", "recover_refinement", "release_refinement"])
    expect(await Bun.file(cli + ".calls").text()).toBe(beforePlanning)
    expect(store.db.query("SELECT * FROM events WHERE task_id=? AND kind='refinement_execution_proposal'").all(saved.task.id)).toHaveLength(1)
    expect(store.get(saved.task.id).approval).toBe(approval)
    await expect(invoke({ action: "release_refinement", reason: "Revise the explicit research budget after failed planning" })).rejects.toThrow("after native planning closure")
    expect((await invoke({ action: "recover_refinement" })).workflow.stage).toBe("CANDIDATE_REVIEW")
    const released = await Bun.file(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json")).json()
    expect(released.refinement_handoff).toBeNull()
    expect(released.refinement_handoff_history).toHaveLength(1)
    expect(released.refinement_handoff_history[0].closure.planning_scope.operation_id).toBe(locked.refinement_handoff.planning_scope.operation_id)
    expect(released.screening_handoff.selection.selection_id).toBe(proof.selection_id)
    expect(await Bun.file(cli + ".calls").text()).toBe(beforePlanning)
    // Fixture an already granted exact native workflow with a lost UI reply.
    // Actual MCP, SQLite, approval validation, cancellation and projection run;
    // no engine or daemon is started by this isolated observer callback.
    const parent = store.get(saved.task.id)
    const selected = verifySelectionProposal(proof, parent, store.stages(parent.id), policy)
    const nativePlan = refinementFixture(parent, selected, f.environment.source)
    const planning = refinementPlanningScope.parse({ ...locked.refinement_handoff.planning_scope, operation_id: "8".repeat(32) })
    const identity = { workflow_id: nativePlan.id, digest: digest(nativePlan), proposal_digest: "6".repeat(64),
      approve_before: nativePlan.stages[0].approve_before }
    const planningReceipt = { schema: "library-workflow-refinement-planning-v1", planning_scope: planning,
      task_id: parent.id, checked_at: Date.now(), proposal: { digest: identity.proposal_digest, state: "PENDING_RUNTIME_CONFIRMATION",
        selected_count: selected.selected_count, proposed_points: nativePlan.max_points, quality_policy_digest: "5".repeat(64), blockers: [],
        stages: nativePlan.stages.map(stage => ({ engine: stage.engine.name, candidate_count: stage.input.candidates.length,
          minutes: stage.minutes, proposed_points: stage.max_points })) },
      draft: { ...identity, state: "COMPILED_NOT_EXECUTABLE", blockers: [] }, compute_submitted: false,
      approval_recorded: false, scientific_assessment: "not_evaluated" }
    const persisted = await Bun.file(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json")).json()
    persisted.stage = "REFINEMENT_PLAN_READY"
    persisted.revision += 1
    persisted.refinement_handoff = { host_scope: locked.refinement_handoff.host_scope, source: f.environment.source,
      task_id: parent.id, planning_scope: planning, selected_count: selected.selected_count, receipt: planningReceipt,
      proposal_digest: identity.proposal_digest, draft_identity: identity }
    await Bun.write(path.join(root, "state/library-workflows", f.intent.workflow_id, "state.json"), JSON.stringify(persisted))
    retainPlanningEvent(store, parent.id, "refinement_workflow_draft",
      { state: "COMPILED_NOT_EXECUTABLE", workflow: nativePlan, digest: digest(nativePlan) }, planning)
    const workflows = new RefinementWorkflows(new Refinements(store))
    workflows.create(nativePlan); workflows.approve(nativePlan.id, digest(nativePlan), nativeContext)
    const nativeGrant = workflows.get(nativePlan.id).approval
    flags.observerFailure = true
    await expect(invoke({ action: "approve_refinement" })).rejects.toThrow("observer startup failed")
    const executing = await invoke({ action: "recover_refinement" })
    expect(executing.workflow.stage).toBe("REFINEMENT_EXECUTION")
    expect(executing).toMatchObject({ refinement_observation: { native_state: "APPROVED", approval_recorded: true,
      execution_state: "not_submitted", remote_artifacts_rehashed: false } })
    expect(executing.workflow.tools.covalent_library_research.actions).not.toContain("approve_refinement")
    const notReady = await invoke({ action: "refinement_report" })
    expect(notReady.workflow.stage).toBe("REFINEMENT_EXECUTION")
    expect(notReady).toMatchObject({ refinement_result: { verification: "not_ready", report: null, compute_submitted: false } })
    expect(JSON.stringify(notReady)).not.toContain("planning_scope")
    expect(notReady.workflow.tools.covalent_library_research.actions).not.toContain("resume_refinement_delivery")
    await expect(invoke({ action: "resume_refinement_delivery" })).rejects.toThrow()
    expect(notReady.workflow.tools.covalent_library_research.actions).not.toContain("select_md_candidate")
    await expect(invoke({ action: "select_md_candidate", compound_id: "fixture-two", input_sdf_index: 2,
      reason: "Candidate choice cannot precede actual refinement and verified publication" })).rejects.toThrow()
    expect(store.db.query("SELECT * FROM refinement_runs").all()).toEqual([])
    await invoke({ action: "wake_refinement" })
    const stopping = await invoke({ action: "cancel_refinement" })
    expect(stopping.workflow.stage).toBe("REFINEMENT_EXECUTION")
    expect(stopping).toMatchObject({ refinement_observation: { execution_state: "outcome_unknown",
      stages: [{ state: "CANCEL_REQUESTED", submission_reserved: false }, { state: "CANCEL_REQUESTED", submission_reserved: false }] } })
    expect(observerStarts).toHaveLength(3)
    expect(workflows.get(nativePlan.id).approval).toBe(nativeGrant)
    expect(store.db.query("SELECT * FROM refinement_workflows").all()).toHaveLength(1)
    expect(await Bun.file(cli + ".calls").text()).toBe(beforePlanning)
  } finally {
    await client.close(); store.close()
    await Storage.remove([libraryStorage, scope.project, scope.sessionID])
    if (prior === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = prior
  }
}, 120000)
