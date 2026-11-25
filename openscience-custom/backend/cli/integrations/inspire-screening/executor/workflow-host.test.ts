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
import { requestHash } from "./request"
import { compileWorkflowDraft, exchangeWorkflow, workflowLedgerIdentity } from "./workflow-host"
import { workflowScreeningIntent } from "./workflow-plan"
import type { z } from "zod"

const workspace = path.resolve(import.meta.dir, "../../../../../..")
const python = process.env.COVSCREEN_TEST_PYTHON ?? path.join(workspace, ".cache/refinement-validation-python310/bin/python")
const source = path.join(workspace, "science")

test("well-formed HMAC forgery is rejected before any backend payload is trusted", async () => {
  await using tmp = await tmpdir({ git: true })
  const key = path.join(tmp.path, "test-only.key")
  await Bun.write(key, "host-signature-forgery-fixture-key-123456789")
  await chmod(key, 0o600)
  const prior = process.env.COVALENT_APPROVAL_KEY_FILE
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  try {
    await expect(exchangeWorkflow({ scope: { project: "fixture", directory: tmp.path, sessionID: "fixture", ledger_id: "a".repeat(32) },
      args: { action: "recover_screening", workflow_id: "library-flow-" + "b".repeat(24), expected_revision: 1 }, operation: "open",
      execute: async () => ({ structuredContent: { _host_handoff: Buffer.from("{}").toString("base64url") + "." + "0".repeat(64) } }),
    })).rejects.toThrow("Invalid workflow backend signature")
  } finally {
    if (prior === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = prior
  }
})

test.each(["release", "migrate_to_vs"] as const)("real MCP/host projection recovers lost commit and %s without transferring approval", async purpose => {
  await using tmp = await tmpdir({ git: true })
  const root = await realpath(tmp.path)
  const key = path.join(root, "fixture-only.key")
  await Bun.write(key, "isolated-workflow-handoff-fixture-key-123456789")
  await chmod(key, 0o600)
  const previous = process.env.COVALENT_APPROVAL_KEY_FILE
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  const env = { ...process.env, PYTHONPATH: source + "/src:" + source + "/tests", COVALENT_APPROVAL_KEY_FILE: key }
  const setup = `import json,sys
from pathlib import Path
from test_receptor_handoff import project
from test_workflow_screening_inputs import registration
from test_workflow_screening_plan import REVIEW
from test_library_workflow import call
from covscreen.library_workflow import RESEARCH
root=Path(sys.argv[1]); initial=project(root)
source,_=registration(root)
source.write_text('SMILES,InChI_Key,Source,CompoundType\\n'+'C,test-only,software-fixture,synthetic\\n'*20001)
reviewed=call(root,initial,RESEARCH,'review_receptor')
frozen=call(root,reviewed,RESEARCH,'prepare_screening_inputs',profile='egfr-c797-acrylamide-research-pilot-v1')
planned=call(root,frozen,RESEARCH,'prepare_screening_plan',scientific_review=REVIEW,max_points=10)
print(json.dumps(planned))`
  const child = Bun.spawn([python, "-B", "-c", setup, root], { env, stdout: "pipe", stderr: "pipe" })
  const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  if (code !== 0) throw Error(err)
  const planned = JSON.parse(out) as { workflow: { workflow_id: string; revision: number } }
  // Only filesystem path metadata is relocated by this explicit test adapter:
  // real local files/parent audits are retained. No claim of remote asset
  // existence; environment and CLI below are bounded software fixtures.
  const bootstrap = `import sys
from pathlib import Path
from covscreen.layout import enable_library_first
enable_library_first()
from covscreen_library_first import screening_plan
original=screening_plan.intent
def relocate(value):
 if isinstance(value,dict): return {k: '/inspire/workflow-host-fixture'+v if k=='path' and isinstance(v,str) else relocate(v) for k,v in value.items()}
 if isinstance(value,list): return [relocate(v) for v in value]
 return value
screening_plan.intent=lambda root,state: relocate(original(root,state))
from covscreen.mcp.server import main
main(Path(sys.argv[1]))`
  const client = new Client({ name: "workflow-host-fixture", version: "1" })
  const store = new Store(path.join(root, "tasks.sqlite"))
  const foreign = new Store(path.join(root, "foreign.sqlite"))
  const session = "fixture-" + crypto.randomUUID()
  const scope = { project: "fixture", directory: root, sessionID: session }
  const cli = path.join(root, "dry-only.ts")
  const calls = path.join(root, "dry-calls.jsonl")
  await Bun.write(cli, `#!${process.execPath}
import { appendFileSync } from 'node:fs';
const args=process.argv.slice(2);
if (!args.includes('--dry-run') || !args.includes('create')) process.exit(71);
appendFileSync(${JSON.stringify(calls)},JSON.stringify(args)+'\\n');
console.log(JSON.stringify({success:true,data:{dry_run:true}}));
`)
  await chmod(cli, 0o700)
  const text = "Software test: prepare a draft only; no scientific computation authorized"
  const request = { text, origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant",
    part_ids: ["fixture-text"], request_sha256: requestHash(text) } }
  const observed = { environments: 0, lost: false, releaseLost: false, observers: 0, approvals: 0, response: undefined as unknown }
  const environment = async (intent: z.infer<typeof workflowScreeningIntent>) => {
    observed.environments++
    const inputs = intent.research_inputs
    const resource = { kind: "hpc" as const, workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
      image: "fixture", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0 }
    return { assets: Object.fromEntries([inputs.library, inputs.library.manifest, inputs.receptor, inputs.reference, inputs.science_config]
        .map(item => [item.path, item.sha256])),
      runtime: { root: "/inspire/workflow-host-fixture/tasks", project: "/inspire/workflow-host-fixture/science",
        worker: "/inspire/workflow-host-fixture/worker", activation: "/inspire/workflow-host-fixture/env.sh" },
      connection: { cli, account: "fixture", notebook: "fixture", workspace: "CPU资源空间" as const },
      cpu: resource, gpu: { ...resource, kind: "job" as const, workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 1 } }
  }
  try {
    await client.connect(new StdioClientTransport({ command: python, args: ["-B", "-c", bootstrap, root], env, stderr: "pipe" }))
    const definitions = await client.listTools()
    const tools: Record<string, Tool> = Object.fromEntries(definitions.tools.map(tool => ["science_" + tool.name,
      dynamicTool({ inputSchema: jsonSchema(tool.inputSchema), execute: args => client.callTool({ name: tool.name,
        arguments: args as Record<string, unknown> }, undefined, { timeout: 120000 }) })]))
    await Storage.write([libraryStorage, scope.project, session], { entry: "science_covalent_library_workflow",
      workflow_id: planned.workflow.workflow_id, active: true })
    const projection = { project: scope.project, session, intent: true, screening: async (args: Record<string, unknown>,
      _options: unknown, execute: (args: Record<string, unknown>) => Promise<unknown>) => compileWorkflowDraft({ store, scope, args,
        source: args.action === "compile_screening" ? request : undefined, environment, signal: new AbortController().signal,
        native: { context: { ...scope, abort: new AbortController().signal, metadata() {},
          async ask() { observed.approvals++; throw Error("Fixture user declined native approval") } },
          observer: async () => { observed.observers++ } },
        execute: async values => {
          const payload = JSON.parse(Buffer.from(String(values._workflow_handoff).split(".")[0], "base64url").toString())
          if (purpose === "migrate_to_vs" && payload.operation === "release" && !observed.releaseLost) {
            observed.releaseLost = true
            throw Error("Fixture lost transport after ledger closure, before backend release")
          }
          const response = await execute(values)
          observed.response = response
          if (payload.operation === "commit" && !observed.lost) { observed.lost = true; throw Error("Fixture lost reply after backend commit") }
          return response
        } }) }
    const initial = await libraryWorkflowTools(tools, projection)
    await expect(initial.science_covalent_library_research.execute!({ action: "compile_screening", _workflow_handoff: "model-proof" },
      { toolCallId: "injected", messages: [] })).rejects.toThrow("unexpected")
    await expect(initial.science_covalent_library_research.execute!({ action: "compile_screening" },
      { toolCallId: "compile", messages: [] })).rejects.toThrow("lost reply")
    expect(store.list()).toHaveLength(1)
    expect(store.list()[0].approval).toBeNull()
    const refreshed = await libraryWorkflowTools(tools, projection)
    const result = await refreshed.science_covalent_library_research.execute!({ action: "recover_screening" }, { toolCallId: "recover", messages: [] })
    expect(result).toHaveProperty("structuredContent.workflow.stage", "SCREENING_EXECUTION_DRAFT")
    expect(result).toHaveProperty("structuredContent.execution.state", "not_submitted")
    expect(JSON.stringify(result)).not.toContain("_host_handoff")
    expect(JSON.stringify(result)).not.toContain("/inspire/workflow-host-fixture")
    expect(observed.environments).toBe(1)
    expect(store.list()).toHaveLength(1)
    expect((await Bun.file(calls).text()).split("\n").filter(Boolean)).toHaveLength(4)
    expect(store.stages(store.list()[0].id).every(stage => stage.handle === null && stage.submitted === null)).toBe(true)
    const current = (result as { structuredContent: { workflow: { workflow_id: string; revision: number } } }).structuredContent.workflow
    const args = { action: "recover_screening" as const, workflow_id: current.workflow_id, expected_revision: current.revision }
    const execute = async (parameters: Record<string, unknown>) => await client.callTool({ name: "covalent_library_research", arguments: parameters })
    await expect(compileWorkflowDraft({ store: foreign, scope, args, execute, environment, signal: new AbortController().signal })).rejects.toThrow()
    expect(foreign.list()).toHaveLength(0)
    expect(workflowLedgerIdentity(foreign)).not.toBe(workflowLedgerIdentity(store))
    await expect(exchangeWorkflow({ scope: { ...scope, ledger_id: workflowLedgerIdentity(store) }, args, operation: "open",
      execute: async () => observed.response })).rejects.toThrow("challenge")
    const changed = structuredClone(observed.response) as { structuredContent: { _host_handoff: string } }
    changed.structuredContent._host_handoff = changed.structuredContent._host_handoff.slice(0, -1) + "x"
    await expect(exchangeWorkflow({ scope: { ...scope, ledger_id: workflowLedgerIdentity(store) }, args, operation: "open",
      execute: async () => changed })).rejects.toThrow("envelope")
    if (purpose === "release") {
      const draft = await libraryWorkflowTools(tools, projection)
      await expect(draft.science_covalent_library_research.execute!({ action: "approve_screening" },
        { toolCallId: "decline", messages: [] })).rejects.toThrow("Fixture user declined")
      expect(observed.approvals).toBe(1)
    } else {
      // Isolated historical ledger fixture: the old exact plan was approved
      // and cancelled before submission. No native prompt or worker is run.
      const task = store.list()[0]
      store.approve(task.id, task.digest, session, root)
      store.cancel(task.id, root)
      store.state(task.id, "CANCELLED", "Historical fixture: no stage submitted")
      expect(observed.approvals).toBe(0)
    }
    expect(observed.observers).toBe(0)
    expect(store.list()[0].state).toBe("CANCELLED")
    const stopped = await libraryWorkflowTools(tools, projection)
    const status = await stopped.science_covalent_library_research.execute!({ action: "screening_status" }, { toolCallId: "status", messages: [] })
    expect(status).toHaveProperty("structuredContent.workflow.stage", "SCREENING_STOPPED")
    expect(status).toHaveProperty("structuredContent.execution.state", "not_submitted")
    expect(status).toHaveProperty("structuredContent.screening_observation.remote_artifacts_rehashed", false)
    const release = await libraryWorkflowTools(tools, projection)
    if (purpose === "migrate_to_vs") {
      await expect(release.science_covalent_library_research.execute!({ action: "migrate_screening_to_vs",
        reason: "Replace suspended engine with a newly approved VS plan" }, { toolCallId: "migrate", messages: [] }))
        .rejects.toThrow("after ledger closure")
      expect(store.list()[0].approval).not.toBeNull()
    }
    const refreshedRelease = await libraryWorkflowTools(tools, projection)
    const released = await refreshedRelease.science_covalent_library_research.execute!(purpose === "migrate_to_vs"
      ? { action: "recover_screening" } : { action: "release_screening", reason: "Revise the declined fixture plan" },
      { toolCallId: "release", messages: [] })
    expect(released).toHaveProperty("structuredContent.workflow.stage", purpose === "migrate_to_vs" ? "VS_SCREENING_PLAN" : "SCREENING_PLAN")
    expect(released).toHaveProperty("structuredContent.result.approval_recorded", false)
    if (purpose === "migrate_to_vs") {
      expect(released).toHaveProperty("structuredContent.result.new_approval_required", true)
      expect(released).toHaveProperty("structuredContent.execution", { scope: "library_vs_screening_planning", state: "not_submitted", task_ids: [] })
    }
    expect(JSON.stringify(released)).not.toContain("_host_handoff")
    expect(JSON.stringify(released)).not.toContain("/inspire/workflow-host-fixture")
    expect(store.list()).toHaveLength(1)
    expect(observed.environments).toBe(1)
    expect(observed.observers).toBe(0)
    expect((await Bun.file(calls).text()).split("\n").filter(Boolean)).toHaveLength(4)
  } finally {
    await client.close(); store.close(); foreign.close()
    await Storage.remove([libraryStorage, scope.project, session])
    if (previous === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = previous
  }
}, 300000)
