import { expect, test } from "bun:test"
import { dynamicTool, jsonSchema, type Tool } from "ai"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm, readFile, readdir, chmod } from "node:fs/promises"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { researchWorkflowTools, projectResearchNativeTools, guardResearchNativeTools } from "../../src/session/research-workflows"
import { libraryWorkflowTools } from "../../src/session/library-workflow"
import { guardCovalentExecution } from "../../src/session/covalent-execution"
import { approveCovalent, covalentPreparer } from "../../src/session/covalent-approval"
import { Storage } from "../../src/storage/storage"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { PermissionNext } from "../../src/permission/next"
import { Bus } from "../../src/bus"
import { SessionTraceStore } from "../../src/session/trace-store"
import { tmpdir } from "../fixture/fixture"
import { workflowScreeningIntent } from "../../integrations/inspire-screening/executor/workflow-plan"
import { createStudy } from "../../integrations/discovery-platform/study-store"
import { bindStudySession } from "../../integrations/discovery-platform/study-session"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const source = path.join(workspace, "science")
const python = (process.env.COVSCREEN_TEST_PYTHON ?? "python3")
const entry = "science_covalent_library_workflow"
const research = "science_covalent_library_research"
const receptor = "science_covalent_library_receptor"
const ligand = "science_covalent_ligand_workflow"
const options = { toolCallId: "library-real-mcp", messages: [] }
const schema = (tool: Tool) => (tool.inputSchema as { jsonSchema: { properties: Record<string, { enum?: string[] }> } }).jsonSchema
const data = (result: unknown) => (result as { structuredContent: { workflow: { workflow_id: string; stage: string; revision: number } } }).structuredContent

test("real MCP De novo projection restores a bound study and retains explicit route navigation", async () => {
  const f = await fixture()
  const scope = { ...f.scope, denovo: { directory: f.root }, text: "de novo design" }
  try {
    const idle = await researchWorkflowTools(f.tools, f.scope)
    expect(idle.activeRoute).toBeUndefined()
    expect(Object.keys(idle.tools).sort()).toEqual([
      "science_covalent_capabilities", "science_covalent_target_hypothesis", entry, ligand,
    ].sort())
    const initial = await researchWorkflowTools(f.tools, scope)
    expect(initial.activeRoute).toBe("de-novo")
    expect(Object.keys(initial.tools).sort()).toEqual([
      "science_covalent_capabilities", "science_covalent_target_hypothesis", entry, ligand,
    ].sort())
    const plan = await createStudy(f.root, f.scope.project, {
      title: "Projection fixture", target: { id: "test", gene: "TEST", uniprot: "TEST", taxon: 9606,
        site: { chain: "A", number: 1, residue: "CYS", atom: "SG", numbering: "canonical" },
        receptor: { reference: "fixture" } }, hypothesis: { state: "pending", rationale: "", evidence: [] },
      routes: { "de-novo": { selected: true, mode: "new", inputs: {} } },
    })
    await bindStudySession({ project: f.scope.project, session: f.scope.session, directory: f.root }, plan.id)
    expect((await researchWorkflowTools(f.tools, { ...scope, text: "继续" })).activeRoute).toBe("de-novo")
    await expect(researchWorkflowTools(f.tools, { ...scope, denovo: { directory: f.root + "-wrong" } }))
      .rejects.toThrow("scope mismatch")
    const switching = await researchWorkflowTools(f.tools, { ...scope, text: "library-first" })
    expect(switching.activeRoute).toBe("library-first")
    expect(Object.keys(switching.tools)).toEqual([entry])
    const native = { denovo_generate: f.tools[entry], discovery_study: f.tools[entry],
      discovery_research: f.tools[entry], inspire_screening_task: f.tools[entry], read: f.tools[entry] }
    expect(Object.keys(projectResearchNativeTools(native, "de-novo"))).toEqual(["denovo_generate", "discovery_study", "discovery_research", "read"])
    for (const route of ["library-first", "ligand-first"])
      expect(Object.keys(projectResearchNativeTools(native, route))).toEqual(["read"])
  } finally {
    await Storage.remove(["study-session-binding", scope.project, scope.session])
    await f.close()
  }
}, 30000)

async function fixture(prepared: boolean | "evidence" = false) {
  const root = await mkdtemp(path.join(os.tmpdir(), "library-workflow-"))
  const key = path.join(root, "fixture-only.key")
  if (prepared === "evidence") {
    await Bun.write(key, "isolated-receptor-evidence-fixture-key-123456")
    await chmod(key, 0o600)
  }
  const environment: Record<string, string> = prepared === "evidence" ? { COVALENT_APPROVAL_KEY_FILE: key } : {}
  const seeded = prepared ? await (async () => {
    const setup = prepared === "evidence" ? "from test_receptor_handoff import project\nfrom test_workflow_screening_inputs import registration\ndef ready(root):\n result = project(root)\n source, _ = registration(root)\n source.write_text('SMILES,InChI_Key,Source,CompoundType\\n' + 'C,test-only,software-fixture,synthetic\\n' * 20001)\n return result" : "from test_library_workflow import ready"
    const child = Bun.spawn([python, "-c", "import json,sys\nfrom pathlib import Path\n" + setup + "\nprint(json.dumps(ready(Path(sys.argv[1]))))", root], {
      env: { ...process.env, ...environment, PYTHONPATH: source + "/src:" + source + "/tests" }, stdout: "pipe", stderr: "pipe",
    })
    const [out, err] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text()])
    if (await child.exited !== 0) throw Error(err)
    return JSON.parse(out) as ReturnType<typeof data>
  })() : undefined
  const client = new Client({ name: "library-workflow-test", version: "1" })
  await client.connect(new StdioClientTransport({ command: python, args: ["-B", path.join(source, "scripts/openscience_mcp_server.py")],
    env: { COVSCREEN_PROJECT_ROOT: root, ...environment }, stderr: "inherit" }))
  const definitions = await client.listTools()
  const tools: Record<string, Tool> = Object.fromEntries(definitions.tools.map(tool => ["science_" + tool.name,
    dynamicTool({ description: tool.description, inputSchema: jsonSchema(tool.inputSchema),
      execute: async args => {
        try { return await client.callTool({ name: tool.name, arguments: args as Record<string, unknown> }, undefined, { timeout: 120000 }) }
        catch (cause) { throw new Error(`MCP ${tool.name} action=${(args as { action?: string }).action} failed`, { cause }) }
      } })]))
  const scope = { project: "library-test", session: "ses-" + crypto.randomUUID(), text: "" }
  return { root, seeded, tools, client, scope, close: async () => {
    await client.close()
    for (const key of ["library-workflow-bindings", "ligand-workflow-bindings"])
      await Storage.remove([key, scope.project, scope.session])
    await rm(root, { recursive: true, force: true })
  } }
}

test("real route selector isolates stages and serializes competing route starts", async () => {
  const f = await fixture()
  try {
    const available = await researchWorkflowTools(f.tools, f.scope)
    expect(available.tools[entry]).toBeDefined()
    expect(available.tools[ligand]).toBeDefined()
    expect(available.tools[research]).toBeUndefined()
    expect(available.tools[receptor]).toBeUndefined()
    expect(available.tools.science_covalent_library_first_chain).toBeUndefined()
    // Use a real harmless MCP status executor behind the native wrapper. It
    // would return a backend error if dispatched; the route guard must reject first.
    const queued = guardResearchNativeTools({ inspire_screening_task: f.tools[entry],
      discovery_study: f.tools[entry], discovery_research: f.tools[entry] }, f.scope)
    const results = await Promise.allSettled([
      available.tools[entry].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options),
      available.tools[ligand].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options),
    ])
    for (const result of results) if (result.status === "fulfilled") expect(result.value).not.toHaveProperty("isError", true)
    expect(results.filter(r => r.status === "fulfilled")).toHaveLength(1)
    expect(results.filter(r => r.status === "rejected")).toHaveLength(1)
    const current = await researchWorkflowTools(f.tools, f.scope)
    expect(current.activeRoute).toBe("library-first")
    await expect(queued.inspire_screening_task.execute!({ action: "status" }, options)).rejects.toThrow("bound execution tools")
    for (const name of ["discovery_study", "discovery_research"])
      await expect(queued[name].execute!({ action: "create" }, options)).rejects.toThrow("bound execution tools")
    await expect(available.tools.science_covalent_capabilities.execute!({}, options)).rejects.toThrow("Another research route")
    expect(Object.keys(current.tools).sort()).toEqual([entry, research].sort())
    expect(schema(current.tools[research]).properties.action.enum).toEqual(["resolve_target"])
    expect(schema(current.tools[research]).properties.workflow_id).toBeUndefined()
    await expect(available.tools[ligand].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options))
      .rejects.toThrow("suspend")
    await current.tools[entry].execute!({ action: "suspend", reason: "Explore another route while retaining this target review" }, options)
    await expect(current.tools[research].execute!({ action: "resolve_target" }, options)).rejects.toThrow("binding changed")
    const suspended = await researchWorkflowTools(f.tools, f.scope)
    expect(suspended.tools[research]).toBeUndefined()
    expect(schema(suspended.tools[entry]).properties.action.enum).toEqual(["resume", "status"])
    await suspended.tools[ligand].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options)
    await expect(suspended.tools[entry].execute!({ action: "resume" }, options)).rejects.toThrow("Another research route")
    const alternative = await researchWorkflowTools(f.tools, f.scope)
    expect(alternative.activeRoute).toBe("ligand-first")
    expect(alternative.tools[receptor]).toBeUndefined()
    await alternative.tools[ligand].execute!({ action: "suspend", reason: "Return to the existing library target review" }, options)
    const returned = await researchWorkflowTools(f.tools, f.scope)
    await returned.tools[entry].execute!({ action: "resume" }, options)
    expect((await researchWorkflowTools(f.tools, f.scope)).activeRoute).toBe("library-first")
  } finally { await f.close() }
}, 30000)

test("prepared receptor recovers from actual backend state and rejects substituted model arguments", async () => {
  const f = await fixture(true)
  try {
    const initial = await researchWorkflowTools(f.tools, { ...f.scope, text: "library-first" })
    expect(Object.keys(initial.tools)).toEqual([entry])
    expect(schema(initial.tools[entry]).properties.request_id).toBeUndefined()
    const restored = await initial.tools[entry].execute!({ action: "status", workflow_id: f.seeded!.workflow.workflow_id }, options)
    expect(data(restored).workflow.stage).toBe("RECEPTOR_PLAN_READY")
    const current = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(current.tools[receptor]).properties.action.enum).toEqual(["plan", "start"])
    for (const name of ["run_id", "human_approved", "approval_note", "workflow_id", "manifest_sha256"])
      expect(schema(current.tools[receptor]).properties[name]).toBeUndefined()
    await expect(current.tools[receptor].execute!({ action: "start", human_approved: true }, options)).rejects.toThrow("unexpected")
    await expect(current.tools[receptor].execute!({ action: "start", workflow_id: "library-flow-" + "f".repeat(24) }, options)).rejects.toThrow("host bound")
    const reopened = await current.tools[entry].execute!({ action: "reconsider", return_to: "structure",
      reason: "Reassess the deposited construct before preparing a receptor" }, options)
    expect(data(reopened).workflow.stage).toBe("STRUCTURE_REVIEW")
    const stale = await current.tools[receptor].execute!({ action: "plan" }, options)
    expect(stale).toHaveProperty("isError", true)
    expect((await researchWorkflowTools(f.tools, f.scope)).tools[receptor]).toBeUndefined()
    const native = { inspire_screening_task: f.tools[entry], denovo_generate: f.tools[entry], read: f.tools[entry] }
    expect(Object.keys(projectResearchNativeTools(native, "library-first"))).toEqual(["read"])
    expect(projectResearchNativeTools(native)).toBe(native)
  } finally { await f.close() }
}, 30000)

test("workflow binding precedes real native plan approval; a rejected card leaves no submission", async () => {
  const f = await fixture(true)
  await using tmp = await tmpdir({ git: true })
  const previous = process.env.COVALENT_APPROVAL_KEY_FILE
  const key = path.join(f.root, "test-only.key")
  await Bun.write(key, "isolated-library-workflow-approval-test-key-123456")
  await chmod(key, 0o600)
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  try {
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const preparations = Object.fromEntries(Object.entries(f.tools).map(([name, tool]) => [name, tool.execute]))
      const guarded = guardCovalentExecution(f.tools, (name, args, opts) => approveCovalent(name, args, session.id, "msg_library", async metadata => {
        expect(metadata.arguments).toMatchObject({ action: "start", workflow_id: f.seeded!.workflow.workflow_id,
          expected_revision: f.seeded!.workflow.revision })
        expect(metadata.execution_plan).toMatchObject({ kind: "job", spec: { engine: "library-first-receptor-preparation",
          request_id: expect.stringContaining(f.seeded!.workflow.workflow_id + "-receptor-r") } })
        await PermissionNext.ask({ id: "permission_library_reject", sessionID: session.id, permission: "remote_compute",
          patterns: [String(metadata.execution_plan_sha256)], always: [], metadata, ruleset: [] })
        throw Error("Rejection unexpectedly returned approval")
      }, prepared => covalentPreparer(name, preparations)(prepared, opts)))
      const initial = await researchWorkflowTools(guarded, f.scope)
      await initial.tools[entry].execute!({ action: "status", workflow_id: f.seeded!.workflow.workflow_id }, options)
      const current = await researchWorkflowTools(guarded, f.scope)
      const statefile = path.join(f.root, "state/library-workflows", f.seeded!.workflow.workflow_id, "state.json")
      const before = await readFile(statefile, "utf8")
      const pending = Promise.withResolvers<PermissionNext.Request>()
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => pending.resolve(event.properties))
      const execution = Promise.resolve(current.tools[receptor].execute!({ action: "start" }, options))
        .then(value => ({ value, error: undefined }), (error: unknown) => ({ value: undefined, error }))
      try {
        // A pre-card failure must terminate this test, rather than hanging on an event.
        const event = await Promise.race([pending.promise, execution.then(result => { throw result.error ?? Error("Missing approval card") })])
        expect(event.metadata.tool).toBe("covalent_library_receptor")
        expect(await readFile(statefile, "utf8")).toBe(before)
        await PermissionNext.reply({ requestID: event.id, reply: "reject" })
        expect((await execution).error).toBeDefined()
        expect((await SessionTraceStore.read(session.id)).approvals[event.id].reply).toBe("reject")
        expect(await readFile(statefile, "utf8")).toBe(before)
        expect(await readdir(path.join(f.root, "state/jobs")).catch(() => [])).toEqual([])
      } finally { unsubscribe() }
    } })
  } finally {
    if (previous === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = previous
    await f.close()
  }
}, 30000)

test("legacy discovery stays compatible; missing bound Library servers fail closed", async () => {
  const f = await fixture()
  try {
    const old = Object.fromEntries(Object.entries(f.tools).filter(([name]) => ![entry, research, receptor].includes(name)))
    expect(await libraryWorkflowTools(old, { ...f.scope, intent: false })).toBe(old)
    await Storage.write(["library-workflow-bindings", f.scope.project, f.scope.session], {
      entry, workflow_id: "library-flow-" + "a".repeat(24), active: true,
    })
    await expect(researchWorkflowTools(old, f.scope)).rejects.toThrow("unavailable")
  } finally { await f.close() }
}, 30000)

test("real MCP binds receptor and screening input roles, restores reads and invalidates dependent assets without compute", async () => {
  const f = await fixture("evidence")
  try {
    const initial = await researchWorkflowTools(f.tools, f.scope)
    await initial.tools[entry].execute!({ action: "status", workflow_id: f.seeded!.workflow.workflow_id }, options)
    const current = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(current.tools[research]).properties.action.enum).toContain("review_receptor")
    expect(schema(current.tools[research]).properties.action.enum).not.toContain("read_screening_receptor")
    expect(schema(current.tools[research]).properties.preparation_run_id).toBeUndefined()
    const before = await readdir(path.join(f.root, "state/jobs"))
    const reviewed = await current.tools[research].execute!({ action: "review_receptor" }, options)
    expect(reviewed).not.toHaveProperty("isError", true)
    expect(reviewed).toHaveProperty("structuredContent.result.result.status", "RECEPTOR_EVIDENCE_REPLAYED")
    expect(reviewed).toHaveProperty("structuredContent.result.result.screening_authorized", false)
    expect(reviewed).not.toHaveProperty("structuredContent.result.result.inventory")
    expect(reviewed).not.toHaveProperty("structuredContent.result.result.receptor.path")
    const restored = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(restored.tools[research]).properties.action.enum).toContain("read_screening_receptor")
    const read = await restored.tools[research].execute!({ action: "read_screening_receptor" }, options)
    expect(data(read).workflow.revision).toBe(data(reviewed).workflow.revision)
    expect(schema(restored.tools[research]).properties.action.enum).toContain("prepare_screening_inputs")
    expect(schema(restored.tools[research]).properties.library_path).toBeUndefined()
    expect(schema(restored.tools[research]).properties.reference_ligand).toBeUndefined()
    await expect(restored.tools[research].execute!({ action: "prepare_screening_inputs",
      profile: "egfr-c797-acrylamide-research-pilot-v1", library_path: "/inspire/injected.csv" }, options)).rejects.toThrow("unexpected")
    const prepared = await restored.tools[research].execute!({ action: "prepare_screening_inputs",
      profile: "egfr-c797-acrylamide-research-pilot-v1" }, options)
    if (prepared && typeof prepared === "object" && "isError" in prepared && prepared.isError)
      throw Error(JSON.stringify(prepared))
    expect(prepared).not.toHaveProperty("isError", true)
    expect(prepared).toHaveProperty("structuredContent.result.reference.component_id", "AQ4")
    expect(prepared).toHaveProperty("structuredContent.result.input_roles.library.records", 20000)
    expect(prepared).toHaveProperty("structuredContent.result.screening_authorized", false)
    expect(prepared).not.toHaveProperty("structuredContent.result.input_roles.receptor.path")
    const persisted = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(persisted.tools[research]).properties.action.enum).toContain("read_screening_inputs")
    const inputs = await persisted.tools[research].execute!({ action: "read_screening_inputs" }, options)
    expect(data(inputs).workflow.revision).toBe(data(prepared).workflow.revision)
    expect(schema(persisted.tools[research]).properties.action.enum).toContain("prepare_screening_plan")
    const planned = await persisted.tools[research].execute!({ action: "prepare_screening_plan", max_points: 0.25,
      scientific_review: {
        construct_and_components: "Historical isolated-chain software fixture; removed ligands and waters limit pocket interpretation.",
        reaction_scope: "Exploratory acrylamide hypothesis; structural geometry alone does not validate cysteine reactivity.",
        prefix_scope: "Deterministic 20,000-record prefix; no random sampling or whole-library coverage claim.",
      } }, options)
    expect(planned).not.toHaveProperty("isError", true)
    expect(data(planned).workflow.stage).toBe("SCREENING_PLAN_READY")
    expect(planned).toHaveProperty("structuredContent.result.budget_status", "proposed_not_user_approved")
    expect(planned).not.toHaveProperty("structuredContent.result.research_inputs")
    const artifact = (planned as { structuredContent: { result: { artifact_id: string } } }).structuredContent.result.artifact_id
    const record = JSON.parse(await readFile(path.join(f.root, "state/screening_plans", artifact.slice(15), "screening-plan.json"), "utf8"))
    // The real backend creates local files for this test. Host production rules
    // correctly reject them; relocating only path fields is a schema fixture,
    // not a claim of remotely installed data or authority to compile/execute.
    expect(workflowScreeningIntent.safeParse(record.intent).success).toBe(false)
    const relocate = (value: unknown): unknown => {
      if (Array.isArray(value)) return value.map(relocate)
      if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) =>
        [key, key === "path" && typeof item === "string" ? "/inspire/workflow-contract-fixture" + item : relocate(item)]))
      return value
    }
    const contract = workflowScreeningIntent.parse(relocate(record.intent))
    expect(contract.revision).toBe(data(planned).workflow.revision)
    expect(contract.conditions.max_points).toBe(0.25)
    expect(contract.scientific_review).toEqual(record.intent.scientific_review)
    const planview = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(planview.tools[research]).properties.action.enum).toContain("read_screening_plan")
    expect(schema(planview.tools[research]).properties.operation_id).toBeUndefined()
    const readplan = await planview.tools[research].execute!({ action: "read_screening_plan" }, options)
    expect(data(readplan).workflow.revision).toBe(data(planned).workflow.revision)
    expect(readplan).toHaveProperty("structuredContent.result.operation_id",
      (planned as { structuredContent: { result: { operation_id: string } } }).structuredContent.result.operation_id)
    await planview.tools[entry].execute!({ action: "reconsider", return_to: "structure",
      reason: "Review another construct before committing screening inputs" }, options)
    const revised = await researchWorkflowTools(f.tools, f.scope)
    expect(schema(revised.tools[research]).properties.action.enum).not.toContain("read_screening_inputs")
    expect(schema(revised.tools[research]).properties.action.enum).not.toContain("read_screening_plan")
    expect(await readdir(path.join(f.root, "state/jobs"))).toEqual(before)
  } finally { await f.close() }
}, 150000)
