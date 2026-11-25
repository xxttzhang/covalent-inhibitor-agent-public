import { expect } from "bun:test"
import path from "node:path"
import { chmod } from "node:fs/promises"
import z from "zod"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { ArtifactStore } from "../../../src/artifact/store"
import { digest } from "./model"
import type { Store } from "./store"
import type { Publications } from "./publication"
import type { BoundRequest } from "./request"
import type { WorkflowScreeningIntent } from "./workflow-plan"
import type { RefinementWorkflow } from "./refinement-workflow"
import { workflowObservation } from "./workflow-execution"
import { hostScope, runWorkflowScreening } from "./workflow-host"
import { refinementApprovalReference } from "./refinement-execution"
import { refinementObservation } from "./refinement-lifecycle"
import { workflowRefinementResult } from "./workflow-refinement-results"
import { screeningResult } from "./workflow-results"

/** Starts at an explicitly synthetic completed scientific state. Every host
 * operation below uses the real native ledger, file API, MCP and pose freezer. */
export async function exerciseMdHost(input: { directory: string; store: Store; publications: Publications;
  scope: z.infer<typeof hostScope>; intent: WorkflowScreeningIntent; source: BoundRequest;
  reference: z.infer<typeof refinementApprovalReference>; workflow: RefinementWorkflow;
  selection: z.infer<typeof screeningResult>["selection"]; core: ReturnType<typeof workflowObservation>;
  report: z.infer<typeof workflowRefinementResult>; observation: z.infer<typeof refinementObservation> }) {
  const root = input.directory
  const workspace = path.resolve(import.meta.dir, "../../../../../..")
  const key = path.join(root, "test-only.key")
  await Bun.write(key, "fixture-refinement-key-not-for-production-123456")
  await chmod(key, 0o600)
  const prior = process.env.COVALENT_APPROVAL_KEY_FILE
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  const draft = { workflow_id: input.workflow.id, digest: digest(input.workflow), proposal_digest: "c".repeat(64),
    state: "COMPILED_NOT_EXECUTABLE", approve_before: input.workflow.stages[0].approve_before, blockers: [] }
  const receipt = { schema: "library-workflow-refinement-planning-v1", planning_scope: input.reference.planning_scope,
    task_id: input.report.task_id, checked_at: Date.now(), draft, proposal: { digest: draft.proposal_digest,
      state: "PENDING_RUNTIME_CONFIRMATION", proposed_points: input.workflow.max_points, selected_count: input.selection!.selected_count,
      quality_policy_digest: digest(input.workflow.stages[0].quality_policy), blockers: [],
      stages: input.workflow.stages.map(stage => ({ engine: stage.engine.name, minutes: stage.minutes,
        candidate_count: stage.input.candidates.length, proposed_points: stage.max_points })) },
    compute_submitted: false, approval_recorded: false, scientific_assessment: "not_evaluated" }
  await Bun.write(path.join(root, "md-host.json"), JSON.stringify({
    workflow_id: input.intent.workflow_id, intent: input.intent, scope: input.scope, source: input.source,
    selection: input.selection, core: input.core,
    refinement: { host_scope: input.scope, source: { text: input.workflow.stages[0].request, origin: input.workflow.stages[0].origin },
      task_id: input.report.task_id, planning_scope: input.reference.planning_scope, selected_count: input.selection!.selected_count,
      receipt, proposal_digest: draft.proposal_digest,
      draft_identity: { workflow_id: draft.workflow_id, digest: draft.digest, proposal_digest: draft.proposal_digest, approve_before: draft.approve_before },
      execution_observation: input.observation },
  }))
  const bootstrap = `import json,os,sys
from pathlib import Path
from pytest import MonkeyPatch
from covscreen.workflow_store import load,save
from covscreen.library_workflow import RESEARCH
from covscreen_library_first.workflow import directory
from covscreen.mcp.server import main
from test_workflow_md_transfer import research,ready
root=Path(sys.argv[1]); meta=json.loads((root/'md-host.json').read_text())
with MonkeyPatch.context() as patch:
    fixture=research.__wrapped__(root,patch)
    ready(fixture,receptor_identity={'entry_id':'SOFTWARE-ONLY','uniprot':'P00533','canonical_cys':797,'author_chain':'A','author_residue':'797'})
    state=load(fixture[1])
state.update(workflow_id=meta['workflow_id'],revision=20,stage='REFINEMENT_REVIEW',md_candidate=None,md_transfer=None,md_input=None)
plan={'id':'screening-plan-'+'a'*32,'sha256':'a'*64}
state.update(screening_plan=plan,screening_handoff={'scope':meta['scope'],'source':meta['source'],'intent':meta['intent'],
 'screening_plan':plan,'receipt':None,'observation':meta['core'],'selection':meta['selection']},refinement_handoff=meta['refinement'])
folder=directory(root,meta['workflow_id']);folder.mkdir(exist_ok=True);save(folder,state)
os.environ['COVALENT_APPROVAL_KEY_FILE']=str(root/'test-only.key')
main(root)
`
  const client = new Client({ name: "md-host-software-contract", version: "1" })
  try {
    await client.connect(new StdioClientTransport({ command: process.env.COVSCREEN_TEST_PYTHON ?? "python3",
      args: ["-B", "-c", bootstrap, root], env: { ...process.env,
        PYTHONPATH: ["science/src", "science/tests"].map(name => path.join(workspace, name)).join(path.delimiter) }, stderr: "inherit" }))
    const state = () => Bun.file(path.join(root, "state/library-workflows", input.intent.workflow_id, "state.json")).json()
    const flags = { chunk: false, frozen: false, trash: false }
    const exchanges: string[] = []
    const frozen = () => digest({ tasks: input.store.list(), stages: input.store.stages(input.report.task_id),
      runs: input.store.db.query("SELECT * FROM refinement_runs ORDER BY id").all(),
      grants: input.store.db.query("SELECT * FROM refinement_workflows ORDER BY id").all(),
      delivery: input.store.db.query("SELECT * FROM refinement_delivery_files ORDER BY name").all() })
    const before = frozen()
    const invoke = async (action: string, values: Record<string, unknown> = {}) => {
      const current = await state()
      return runWorkflowScreening({ store: input.store, scope: input.scope,
        args: { action, workflow_id: input.intent.workflow_id, expected_revision: current.revision, ...values },
        signal: new AbortController().signal, native: { publications: input.publications,
          context: { directory: root, sessionID: input.scope.sessionID, abort: new AbortController().signal,
            metadata() {}, async ask() { throw Error("MD handoff must not request compute approval") } } },
        execute: async args => {
          const packet = JSON.parse(Buffer.from(String(args._workflow_handoff).split(".")[0], "base64url").toString())
          exchanges.push(packet.operation)
          const result = await client.callTool({ name: "covalent_library_research", arguments: args })
          if (!result.isError && packet.operation === "md_transfer_chunk") {
            if (flags.chunk) { flags.chunk = false; throw Error("Lost chunk acknowledgement after commit") }
            if (flags.trash) {
              flags.trash = false
              const choice = (await state()).md_candidate
              await ArtifactStore.trash(input.scope.project, choice.files.candidate_table.artifact_id)
            }
          }
          if (!result.isError && packet.operation === "md_transfer_commit" && flags.frozen) {
            flags.frozen = false; throw Error("Lost frozen-input acknowledgement after commit")
          }
          return result
        } })
    }
    const report = await invoke("refinement_report")
    expect(report.structuredContent).toHaveProperty("workflow.stage", "REFINEMENT_CANDIDATE_REVIEW")
    const candidate = input.report.report!.candidates.find(row => row.status === "BOTH_STRUCTURAL_QC_PASSED")!
    await invoke("select_md_candidate", { compound_id: candidate.compound_id, input_sdf_index: candidate.input_sdf_index,
      reason: "Exercise the actual host handoff with explicit software evidence and no scientific claims" })
    flags.chunk = true
    // Await the MCP roundtrip first: Bun's rejects matcher stalls stdio reads here.
    const interrupted = await invoke("prepare_md_input").catch(error => error)
    expect(interrupted).toBeInstanceOf(Error)
    expect(interrupted.message).toContain("Lost chunk acknowledgement")
    const partial = await state()
    expect(partial.md_transfer.offsets.candidate_table).toBeGreaterThan(0)
    expect(partial.md_input).toBeNull()
    flags.trash = true
    expect(await invoke("prepare_md_input").catch(error => error)).toBeInstanceOf(Error)
    const rejected = await state()
    expect(rejected.refinement_handoff.report_result.delivery.verification).toBe("evidence_rejected")
    expect(rejected.md_input).toBeNull()
    expect(rejected.md_candidate.id).toBe(partial.md_candidate.id)
    await ArtifactStore.restore(input.scope.project, rejected.md_candidate.files.candidate_table.artifact_id)
    await invoke("refinement_report")
    flags.frozen = true
    const lost = await invoke("prepare_md_input").catch(error => error)
    expect(lost).toBeInstanceOf(Error)
    expect(lost.message).toContain("Lost frozen-input acknowledgement")
    const retained = (await state()).md_input
    expect(retained.md_system_ready).toBe(false)
    const offset = exchanges.length
    const completed = await invoke("prepare_md_input")
    expect(completed.structuredContent).toHaveProperty("md_input", retained)
    expect(completed.structuredContent).toHaveProperty("md_candidate.state", "selected_input_frozen")
    expect(exchanges.slice(offset)).not.toContain("md_transfer_chunk")
    expect(JSON.stringify(completed)).not.toContain("_host_handoff")
    expect(JSON.stringify(completed)).not.toContain("native_target_context")
    expect(JSON.stringify(completed)).not.toContain("/inspire/")
    expect(frozen()).toBe(before)
    const file = (await state()).md_candidate.files.candidate_table
    const artifact = await ArtifactStore.get(input.scope.project, file.artifact_id)
    await ArtifactStore.save({ projectID: input.scope.project, sessionID: input.scope.sessionID,
      sourcePath: artifact!.current.sourcePath, filename: artifact!.current.filename, kind: artifact!.kind,
      content: new Blob(["A new user-controlled version, not the selected candidate table"]), captureQuality: "exact" })
    expect(await invoke("prepare_md_input").catch(error => error)).toBeInstanceOf(Error)
    expect((await state()).refinement_handoff.report_result.delivery.verification).toBe("evidence_rejected")
    expect((await state()).md_input).toEqual(retained)
    expect(frozen()).toBe(before)
  } finally {
    await client.close()
    if (prior === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = prior
  }
}
