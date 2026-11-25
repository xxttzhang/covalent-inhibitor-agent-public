import { expect, test } from "bun:test"
import { dynamicTool, jsonSchema, type Tool, type JSONSchema7 } from "ai"
import path from "node:path"
import { mkdtemp, rm, mkdir, copyFile, readFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import os from "node:os"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { bindLigandArguments, ligandIntent, ligandWorkflowTools, projectLigandTools, workflowView } from "../../src/session/ligand-workflow"
import { libraryIntent } from "../../src/session/library-workflow"
import { denovoIntent } from "../../src/session/denovo-entry"
import { Storage } from "../../src/storage/storage"
import { ArtifactStore } from "../../src/artifact/store"
import { createWorkflowProjection, declaredInterface, workflowInterface } from "../../src/session/route-workflow"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const python = (process.env.COVSCREEN_TEST_PYTHON ?? "python3")
const source = path.join(workspace, "science")
const entry = "science_covalent_ligand_workflow"
const evidence = "science_covalent_ligand_evidence"
const design = "science_covalent_ligand_design_step"
const options = { toolCallId: "test-real-workflow", messages: [] }
const schema = (tool: Tool) => (tool.inputSchema as { jsonSchema: { properties: Record<string, { enum?: string[] }>; $defs?: unknown } }).jsonSchema
type DeclaredSchema = JSONSchema7 & { "x-research-workflow"?: unknown }
const declaredSchema = (tool: Tool) => (tool.inputSchema as { jsonSchema: DeclaredSchema }).jsonSchema
const withDeclaration = (schema: JSONSchema7, declaration: unknown): DeclaredSchema =>
  ({ ...schema, "x-research-workflow": declaration })

test("interrupted local work exposes reconciliation and keeps the original host binding", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  try {
    const initial = await ligandWorkflowTools(f.tools, scope)
    const started = await initial[entry].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options)
    const id = workflowView(started)!.workflow_id
    const child = Bun.spawn([python, "-c", `import os,sys\nfrom pathlib import Path\nfrom covscreen.workflow_store import load\nfrom covscreen.workflow_protocol import begin_operation\nfrom covscreen.ligand_parent_evidence import version_lock\nout=Path(sys.argv[1])/'state/ligand-workflows'/sys.argv[2]\nwith version_lock(out,'.workflow.lock'):\n begin_operation(out,load(out),'covalent_ligand_evidence','resolve_target')\n os._exit(73)`, f.root, id],
      { env: { ...process.env, PYTHONPATH: source + "/src" }, stdout: "pipe", stderr: "pipe" })
    expect(await child.exited).toBe(73)
    const interrupted = await ligandWorkflowTools(f.tools, scope)
    expect(Object.keys(interrupted)).toEqual([entry])
    expect(schema(interrupted[entry]).properties.action.enum).toContain("reconcile")
    expect(schema(interrupted[entry]).properties.reason).toBeUndefined()
    expect(schema(interrupted[entry]).properties.branch_index).toBeUndefined()
    const restored = await interrupted[entry].execute!({ action: "reconcile" }, options)
    expect(restored).not.toHaveProperty("isError", true)
    expect(workflowView(restored)?.workflow_id).toBe(id)
    expect(workflowView(restored)?.revision).toBe(2)
    const resumed = await ligandWorkflowTools(f.tools, scope)
    expect(schema(resumed[evidence]).properties.action.enum).toEqual(["resolve_target"])
    expect(schema(resumed[entry]).properties.action.enum).not.toContain("start")
    expect(schema(resumed[entry]).properties.action.enum).not.toContain("reconcile")
    const repeated = await interrupted[entry].execute!({ action: "reconcile" }, options)
    expect(repeated).toHaveProperty("isError", true)
    expect(JSON.stringify(repeated)).toContain("STALE_WORKFLOW_REVISION")
    const state = JSON.parse(await readFile(path.join(f.root, "state/ligand-workflows", id, "state.json"), "utf8"))
    expect(state.target_identity).toBeNull()
    expect(state.pending).toBeNull()
    expect(state.events.at(-1).operation).toBe("local_operation_reconciled")
  } finally {
    await f.client.close()
    await Storage.remove(["ligand-workflow-bindings", scope.project, scope.session])
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "ligand-workflow-"))
  const child = Bun.spawn([python, "-c", `import json,sys\nfrom pathlib import Path\nfrom test_ligand_parent_evidence import dataset\nfrom covscreen.ligand_parent_evidence import seal\nroot=Path(sys.argv[1]); evidence,comparison=dataset()\nid='hyp-'+'a'*24; out=root/'results/target-hypotheses'/id; out.mkdir(parents=True)\nseal(id,out,evidence)\nh={'route':'ligand-first','rationale':'Synthetic integration fixture','claims':[{'statement':'Fixture comparison','evidence_ids':['PMID:1'],'interpretation':'inference'}],'parent_ligand':'ParentAlpha','parent_binding_mode':'noncovalent','parent_evidence_ids':['PMID:1'],'warhead_class':['acrylamide'],'uncertainties':['No scientific acceptance'],'parent_comparison':comparison}\nprint(json.dumps(h))`, root], {
    env: { ...process.env, PYTHONPATH: source + "/src:" + source + "/tests" }, stdout: "pipe", stderr: "pipe",
  })
  const text = await new Response(child.stdout).text()
  expect(await child.exited).toBe(0)
  const hypothesis = JSON.parse(text)
  const client = new Client({ name: "ligand-workflow-test", version: "1" })
  await client.connect(new StdioClientTransport({ command: python, args: ["-B", path.join(source, "scripts/openscience_mcp_server.py")],
    env: { COVSCREEN_PROJECT_ROOT: root }, stderr: "pipe" }))
  const definitions = await client.listTools()
  const tools: Record<string, Tool> = Object.fromEntries(definitions.tools.map((tool) => ["science_" + tool.name,
    dynamicTool({ description: tool.description, inputSchema: jsonSchema(tool.inputSchema),
      execute: (args) => client.callTool({ name: tool.name, arguments: args as Record<string, unknown> }) })]))
  return { root, hypothesis, client, tools }
}

test("backend interface declaration controls host projection and malformed contracts never fall back", async () => {
  const f = await fixture()
  try {
    const original = declaredSchema(f.tools[entry])
    const declared = workflowInterface.parse(original["x-research-workflow"])
    expect(declared.entry).toBe("covalent_ligand_workflow")
    const updated = { ...declared, legacy_names: [...declared.legacy_names, "covalent_capabilities"] }
    const tools = { ...f.tools, [entry]: { ...f.tools[entry],
      inputSchema: jsonSchema(withDeclaration(original, updated)) } }
    expect(projectLigandTools(tools)["science_covalent_capabilities"]).toBeUndefined()
    const visible = projectLigandTools(tools, entry)
    expect(declaredSchema(visible[entry])["x-research-workflow"]).toBeUndefined()
    const malformed = { ...f.tools, [entry]: { ...f.tools[entry],
      inputSchema: jsonSchema(withDeclaration(original, { ...declared, route: "de-novo" })) } }
    expect(() => declaredInterface(malformed, declared)).toThrow("changed route or entry")
    const missing = { ...f.tools, [entry]: { ...f.tools[entry],
      inputSchema: jsonSchema(withDeclaration(original, { ...declared, tools: [] })) } }
    expect(() => declaredInterface(missing, declared)).toThrow()
    const current = await f.client.callTool({ name: declared.entry,
      arguments: { action: "start", request_id: "interface-probe", target: "EGFR", site: "C797", taxon: 9606 } })
    const projector = createWorkflowProjection(declared, { key: "interface-probe", requestSuffix: "probe" })
    expect(projector.workflowView(current)?.stage).toBe("INTAKE")
    const payload = current.structuredContent
    if (!payload || typeof payload !== "object") throw Error("Expected structured workflow result")
    expect(projector.workflowView({ ...current, structuredContent: { ...payload, route: "library-first" } })).toBeUndefined()
  } finally { await f.client.close(); await rm(f.root, { recursive: true, force: true }) }
}, 30000)

test("real MCP workflow: one entry, stage schemas, bound identity, durable resume and parent reconsideration", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  try {
    const initial = await ligandWorkflowTools(f.tools, scope)
    expect(Object.keys(initial)).toEqual([entry])
    expect(schema(initial[entry]).properties.request_id).toBeUndefined()
    const started = await initial[entry].execute!({ action: "start", target: "TEST", site: "C1", taxon: 9606,
      evidence_id: "hyp-" + "a".repeat(24) }, options)
    expect(workflowView(started)?.stage).toBe("PARENT_REVIEW")
    const reviewed = await ligandWorkflowTools(f.tools, { ...scope, intent: false })
    expect(Object.keys(reviewed).sort()).toEqual([entry, evidence].sort())
    expect(schema(reviewed[evidence]).properties.workflow_id).toBeUndefined()
    expect(schema(reviewed[evidence]).properties.action.enum).toContain("finalize")
    const finalized = await reviewed[evidence].execute!({ action: "finalize", hypothesis: f.hypothesis }, options)
    expect(workflowView(finalized)?.stage).toBe("DESIGN_READY")
    const ready = await ligandWorkflowTools(f.tools, { ...scope, intent: false })
    expect(Object.keys(ready).sort()).toEqual([entry, evidence, design].sort())
    expect(schema(ready[design]).properties.action.enum).toEqual(["prepare"])
    expect(schema(ready[evidence]).properties.action.enum).not.toContain("finalize")
    expect(schema(ready[evidence]).properties.action.enum).toContain("supplement")
    expect(schema(ready[evidence]).properties.hypothesis).toBeUndefined()
    expect(schema(ready[evidence]).$defs).toBeUndefined()
    expect(schema(ready[evidence]).properties.action.enum).toContain("export_parent_report")
    const delivered = await ready[evidence].execute!({ action: "export_parent_report" }, options)
    expect(workflowView(delivered)?.revision).toBe(workflowView(finalized)?.revision)
    expect(JSON.stringify(delivered)).toContain("PARENT_REPORT_DELIVERED")
    expect(JSON.stringify(delivered)).not.toContain('"base64"')
    const first = (await ArtifactStore.list(scope.project)).filter(item => item.current.sessionID === scope.session)
    expect(first).toHaveLength(2)
    await ready[evidence].execute!({ action: "export_parent_report" }, options)
    for (const artifact of first) {
      expect((await ArtifactStore.get(scope.project, artifact.id))?.versionCount).toBe(1)
      const saved = await ArtifactStore.read(scope.project, artifact.id)
      const original = await readFile(path.join(f.root, "results/target-hypotheses", ...artifact.current.sourcePath.split("/").slice(-3)))
      expect(Buffer.from(await saved!.content.arrayBuffer()).equals(original)).toBe(true)
    }
    await expect(ready[design].execute!({ action: "generate" }, options)).rejects.toThrow("unavailable")
    await expect(ready[design].execute!({ action: "prepare", workflow_id: "ligand-flow-" + "f".repeat(24) }, options))
      .rejects.toThrow("host bound")
    const reopened = await ready[entry].execute!({ action: "reconsider", reason: "Review the alternative parent before committing to design" }, options)
    expect(workflowView(reopened)?.stage).toBe("PARENT_REVIEW")
    const repeat = await reviewed[evidence].execute!({ action: "finalize", hypothesis: f.hypothesis }, options)
    expect(repeat).toHaveProperty("isError", true) // Old visible schema cannot bypass backend revision checks.
    const restored = await ligandWorkflowTools(f.tools, { ...scope, intent: false })
    expect(Object.keys(restored).sort()).toEqual([entry, evidence].sort())
    const other = await ligandWorkflowTools(f.tools, { ...scope, session: scope.session + "-other" })
    expect(Object.keys(other)).toEqual([entry])
    const hidden = ["science_covalent_ligand_first_campaign", "science_covalent_submit_job", "science_covalent_ligand_first_reuse_result"]
    for (const name of hidden) expect(restored[name]).toBeUndefined()
    // Raw backend capabilities still exist for operators and other routes.
    for (const name of hidden) expect(f.tools[name]).toBeDefined()
    const suspended = await restored[entry].execute!({ action: "suspend", reason: "Compare a different research route while preserving this parent review" }, options)
    expect(workflowView(suspended)?.stage).toBe("PARENT_REVIEW")
    const switched = await ligandWorkflowTools(f.tools, scope)
    expect(switched.science_covalent_library_first_chain).toBeDefined()
    expect(switched.science_covalent_plan_de_novo).toBeDefined()
    expect(switched.science_covalent_plan_attachments).toBeDefined()
    expect(schema(switched[entry]).properties.action.enum).toEqual(["resume", "status"])
    await switched[entry].execute!({ action: "status" }, options)
    expect((await ligandWorkflowTools(f.tools, scope)).science_covalent_library_first_chain).toBeDefined()
    const resumed = await switched[entry].execute!({ action: "resume" }, options)
    expect(workflowView(resumed)?.workflow_id).toBe(workflowView(started)?.workflow_id)
    expect(Object.keys(await ligandWorkflowTools(f.tools, scope)).sort()).toEqual([entry, evidence].sort())
  } finally {
    await f.client.close()
    await Storage.remove(["ligand-workflow-bindings", scope.project, scope.session])
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)

test("entry discovery leaves other routes compatible and rejects unscoped Ligand-first finalization", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: false }
  try {
    const tools = await ligandWorkflowTools(f.tools, scope)
    expect(tools.science_covalent_library_first_chain).toBeDefined()
    expect(tools.science_covalent_plan_attachments).toBeDefined()
    expect(tools.science_covalent_enumerate_warheads).toBeDefined()
    expect(tools.science_covalent_ligand_first_chain).toBeUndefined()
    expect(tools[evidence]).toBeUndefined()
    expect(tools[entry]).toBeDefined()
    await expect(tools.science_covalent_target_hypothesis.execute!({ action: "finalize", hypothesis: f.hypothesis }, options))
      .rejects.toThrow("Use covalent_ligand_workflow")
    const legacy = Object.fromEntries(Object.entries(f.tools).filter(([name]) => name !== entry))
    expect(projectLigandTools(legacy)).toBe(legacy)
  } finally { await f.client.close(); await rm(f.root, { recursive: true, force: true }) }
}, 30000)

test("connector recovery preserves the original workflow without starting another study", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  const key = ["ligand-workflow-bindings", scope.project, scope.session]
  try {
    const initial = await ligandWorkflowTools(f.tools, scope)
    const started = await initial[entry].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options)
    const id = workflowView(started)!.workflow_id
    const file = path.join(f.root, "state/ligand-workflows", id, "state.json")
    const before = await readFile(file, "utf8")
    const binding = await Storage.read<Record<string, unknown>>(key)
    await expect(ligandWorkflowTools({}, scope)).rejects.toThrow("unavailable")
    expect(await Storage.read<Record<string, unknown>>(key)).toEqual(binding)
    const restored = await ligandWorkflowTools(f.tools, scope)
    expect(schema(restored[entry]).properties.action.enum).not.toContain("start")
    expect(await readFile(file, "utf8")).toBe(before)
    expect(await Storage.read<Record<string, unknown>>(key)).toEqual(binding)
    await expect(restored[entry].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options))
      .rejects.toThrow()
    expect(await readFile(file, "utf8")).toBe(before)
  } finally {
    await f.client.close()
    await Storage.remove(key)
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)

test("missing bound server fails closed; visibility intent never grants compute", async () => {
  const session = "ses-" + crypto.randomUUID()
  const key = ["ligand-workflow-bindings", "ligand-test", session]
  try {
    await Storage.write(key, { entry, workflow_id: "ligand-flow-" + "a".repeat(24) })
    await expect(ligandWorkflowTools({}, { project: "ligand-test", session, intent: false })).rejects.toThrow("unavailable")
    expect(ligandIntent("针对 EFGR，设计共价抑制剂，ligand-first 路线")).toBe(true)
    expect(ligandIntent("比较 ligand-first 与 library-first")).toBe(false)
    expect(ligandIntent("帮我针对 EGFR C797 基于先导分子进行共价化改造")).toBe(true)
    expect(ligandIntent("Please perform lead covalentization for BTK")).toBe(true)
    expect(libraryIntent("从共价化合物库中筛选 EGFR C797 抑制剂")).toBe(true)
    expect(libraryIntent("Run virtual screening on CovInDB")).toBe(true)
    expect(denovoIntent("针对 EGFR C797 全新骨架设计共价抑制剂")).toBe(true)
    expect(denovoIntent("Pocket-based covalent generation for KRAS")).toBe(true)
    expect(ligandIntent("先从化合物库筛选，再基于先导分子共价化改造")).toBe(false)
    expect(libraryIntent("先从化合物库筛选，再基于先导分子共价化改造")).toBe(false)
    expect(() => bindLigandArguments("covalent_ligand_design_step", { action: "generate" }, session)).toThrow("entry")
  } finally { await Storage.remove(key) }
})

test("mixed-version connectors cannot reopen legacy Ligand-first entries; duplicate primaries fail closed", async () => {
  const f = await fixture()
  const g = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: false }
  try {
    const other = Object.fromEntries(Object.entries(g.tools).map(([key, value]) => [key.replace(/^science_/, "other_"), value]))
    const old = Object.fromEntries(Object.entries(other).filter(([key]) =>
      !["other_covalent_ligand_workflow", "other_covalent_ligand_evidence", "other_covalent_ligand_design_step"].includes(key)))
    const available = await ligandWorkflowTools({ ...f.tools, ...old }, scope)
    expect(available.other_covalent_ligand_design).toBeUndefined()
    expect(available.other_covalent_ligand_first_chain).toBeUndefined()
    expect(available.other_covalent_library_first_chain).toBeDefined()
    await expect(available.other_covalent_target_hypothesis.execute!({ action: "finalize", hypothesis: f.hypothesis }, options))
      .rejects.toThrow("Use covalent_ligand_workflow")
    expect(Object.keys(await ligandWorkflowTools({ ...f.tools, ...old }, { ...scope, intent: true }))).toEqual([entry])
    await expect(ligandWorkflowTools({ ...f.tools, ...other }, { ...scope, intent: true }))
      .rejects.toThrow("exactly one primary")
    const unrelated = await ligandWorkflowTools({ ...f.tools, ...other }, scope)
    expect(unrelated[entry]).toBeUndefined()
    expect(unrelated.other_covalent_ligand_workflow).toBeUndefined()
    expect(unrelated.science_covalent_plan_de_novo).toBeDefined()
    // The backend implementations are still registered and callable by operators.
    expect(old.other_covalent_ligand_design).toBeDefined()
  } finally {
    await f.client.close(); await g.client.close()
    await rm(f.root, { recursive: true, force: true }); await rm(g.root, { recursive: true, force: true })
  }
}, 30000)

test("target-only entry exposes identity review before site-bound evidence collection", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  try {
    const out = path.join(f.root, "state/target_identity", "a".repeat(32))
    await mkdir(out, { recursive: true })
    for (const [from, to] of [["egfr-manifest.json", "target_identity.json"], ["egfr.json", "uniprot.json"], ["P00533.fasta", "P00533.fasta"]])
      await copyFile(path.join(source, "tests/fixtures/target_identity", from), path.join(out, to))
    const digest = createHash("sha256").update(await readFile(path.join(out, "target_identity.json"))).digest("hex")
    const initial = await ligandWorkflowTools(f.tools, scope)
    const started = await initial[entry].execute!({ action: "start", target: "EGFR", taxon: 9606 }, options)
    expect(workflowView(started)?.stage).toBe("TARGET_REVIEW")
    const target = await ligandWorkflowTools(f.tools, scope)
    expect(schema(target[evidence]).properties.action.enum).toEqual(["resolve_target"])
    expect(target[design]).toBeUndefined()
    expect(schema(target[evidence]).properties.hypothesis).toBeUndefined()
    const resolved = await target[evidence].execute!({ action: "resolve_target", target_artifact_id: "target-" + "a".repeat(32),
      target_manifest_sha256: digest }, options)
    expect(workflowView(resolved)?.stage).toBe("TARGET_REVIEW")
    const selection = await ligandWorkflowTools(f.tools, scope)
    expect(schema(selection[entry]).properties.action.enum).toContain("select_site")
    expect(schema(selection[entry]).properties.site).toBeDefined()
    const chosen = await selection[entry].execute!({ action: "select_site", site: "C797",
      reason: "Explore a verified canonical cysteine; structural and mechanism evidence remain to be assessed" }, options)
    expect(workflowView(chosen)?.stage).toBe("INTAKE")
    const collect = await ligandWorkflowTools(f.tools, scope)
    expect(schema(collect[evidence]).properties.action.enum).toEqual(["collect"])
    expect(schema(collect[entry]).properties.action.enum).not.toContain("select_site")
  } finally {
    await f.client.close()
    await Storage.remove(["ligand-workflow-bindings", scope.project, scope.session])
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)

test("queued evidence reads follow a committed successor, but finalization never silently rebases", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  try {
    const initial = await ligandWorkflowTools(f.tools, scope)
    await initial[entry].execute!({ action: "start", target: "TEST", site: "C1", taxon: 9606,
      evidence_id: "hyp-" + "a".repeat(24) }, options)
    const before = await ligandWorkflowTools(f.tools, scope)
    const child = Bun.spawn([python, "-c", `import sys\nfrom pathlib import Path\nfrom covscreen.ligand_parent_evidence import branch,seal\nroot=Path(sys.argv[1]); identifier,out,data=branch(root,'hyp-'+'a'*24)\ndata['supplemental_sources']['PMID:3']={'kind':'literature','title':'Additional synthetic contract source'}\nseal(identifier,out,data)\nprint(identifier)`, f.root],
      { env: { ...process.env, PYTHONPATH: source + "/src" }, stdout: "pipe", stderr: "pipe" })
    const successor = (await new Response(child.stdout).text()).trim()
    expect(await child.exited).toBe(0)
    const observations = await Promise.all([before[evidence].execute!({ action: "read" }, options),
      before[evidence].execute!({ action: "read_parent_evidence" }, options)])
    for (const observation of observations) {
      expect(observation).not.toHaveProperty("isError", true)
      expect(workflowView(observation)?.stage).toBe("PARENT_REVIEW")
      expect(JSON.stringify(observation)).toContain(successor)
    }
    expect(workflowView(observations[0])?.revision).toBe(workflowView(observations[1])?.revision)
    const stale = await before[evidence].execute!({ action: "finalize", hypothesis: f.hypothesis }, options)
    expect(stale).toHaveProperty("isError", true)
    expect(JSON.stringify(stale)).toContain("STALE_WORKFLOW_REVISION")
    const updated = await ligandWorkflowTools(f.tools, scope)
    const ready = await updated[evidence].execute!({ action: "finalize", hypothesis: f.hypothesis }, options)
    expect(workflowView(ready)?.stage).toBe("DESIGN_READY")
  } finally {
    await f.client.close()
    await Storage.remove(["ligand-workflow-bindings", scope.project, scope.session])
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)

test("real MCP exports archived design originals through host Results", async () => {
  const f = await fixture()
  const scope = { project: "ligand-test", session: "ses-" + crypto.randomUUID(), intent: true }
  try {
    const initial = await ligandWorkflowTools(f.tools, scope)
    const started = await initial[entry].execute!({ action: "start", target: "TEST", site: "C1", taxon: 9606,
      evidence_id: "hyp-" + "a".repeat(24) }, options)
    const id = workflowView(started)!.workflow_id
    // Isolated sealed delivery fixture; this does not assert scientific qualification.
    const child = Bun.spawn([python, "-c", `import json,sys\nfrom pathlib import Path\nfrom test_ligand_parent_delivery import design_delivery\nroot=Path(sys.argv[1]); _,folder,fixture=design_delivery.__wrapped__(root)\np=root/'state/ligand-workflows'/sys.argv[2]/'state.json'\ns=json.loads(p.read_text());s['branches']=[{'design_id':fixture['design_id']}];p.write_text(json.dumps(s))`, f.root, id],
      { env: { ...process.env, PYTHONPATH: source + "/src:" + source + "/tests" }, stdout: "pipe", stderr: "pipe" })
    expect(await child.exited).toBe(0)
    const available = await ligandWorkflowTools(f.tools, scope)
    expect(schema(available[evidence]).properties.action.enum).toContain("export_design_report")
    const args = { action: "export_design_report", design_id: "design-" + "d".repeat(24) }
    const delivered = await available[evidence].execute!(args, options)
    expect(delivered).not.toHaveProperty("isError", true)
    expect(workflowView(delivered)?.revision).toBe(workflowView(started)?.revision)
    expect(JSON.stringify(delivered)).toContain("DESIGN_REPORT_DELIVERED")
    expect(JSON.stringify(delivered)).not.toContain('"base64"')
    await available[evidence].execute!(args, options)
    const artifacts = (await ArtifactStore.list(scope.project)).filter(x => x.current.sessionID === scope.session)
    expect(artifacts).toHaveLength(2)
    for (const artifact of artifacts) {
      expect(artifact.versionCount).toBe(1)
      const saved = await ArtifactStore.read(scope.project, artifact.id)
      const original = await readFile(path.join(f.root, "results/ligand-designs", args.design_id, "delivery", saved!.info.filename))
      expect(Buffer.from(await saved!.content.arrayBuffer()).equals(original)).toBe(true)
    }
    const invalid = await available[evidence].execute!({ ...args, design_id: "design-" + "f".repeat(24) }, options)
    expect(invalid).toHaveProperty("isError", true)
  } finally {
    await f.client.close()
    await Storage.remove(["ligand-workflow-bindings", scope.project, scope.session])
    await rm(f.root, { recursive: true, force: true })
  }
}, 30000)
