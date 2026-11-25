import { expect, test } from "bun:test"
import { dynamicTool, jsonSchema, type JSONSchema7 } from "ai"
import path from "node:path"
import { createWorkflowProjection, workflowInterface } from "../../src/session/route-workflow"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const python = process.env.COVSCREEN_TEST_PYTHON ?? "python3"
const result = Bun.spawnSync([python, "-c", "import json\nfrom covscreen.mcp.library_workflow import TOOLS\nprint(json.dumps(TOOLS))"], {
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: path.join(workspace, "science/src") },
})
if (result.exitCode !== 0) throw Error(result.stderr.toString())
const definitions = JSON.parse(result.stdout.toString()) as { name: string; inputSchema: JSONSchema7 }[]
const entry = definitions.find(tool => tool.name === "covalent_library_workflow")!
const profile = workflowInterface.parse((entry.inputSchema as Record<string, unknown>)["x-research-workflow"])
const projection = createWorkflowProjection(profile, { key: "constraint-test", requestSuffix: "test" })
const tools = Object.fromEntries(definitions.map(tool => ["science_" + tool.name, dynamicTool({ inputSchema: jsonSchema(tool.inputSchema), execute: async () => { throw Error("Schema-only fixture must not execute") } })]))
const research = "science_covalent_library_research"
const view = (constraints: Record<string, Record<string, unknown>>) => ({
  protocol: profile.protocol, workflow_id: "library-flow-" + "a".repeat(24), revision: 1, stage: "MD_SHORT_QC_PLAN_READY",
  tools: { covalent_library_research: { actions: ["plan_md_short_qc"], fields: ["action", "resource_budget"], field_constraints: constraints } },
})

function accepts(schema: JSONSchema7, input: unknown) {
  const checked = Bun.spawnSync([python, "-c", "import json,sys\nfrom jsonschema import Draft202012Validator\nschema,value=json.load(sys.stdin)\nDraft202012Validator.check_schema(schema)\nprint(json.dumps(Draft202012Validator(schema).is_valid(value)))"],
    { stdin: Buffer.from(JSON.stringify([schema, input])) })
  if (checked.exitCode !== 0) throw Error(checked.stderr.toString())
  return JSON.parse(checked.stdout.toString()) as boolean
}

const budget = { max_runtime_minutes: 30, cpu_count: 2, memory_gb: 4, gpu_count: 0, max_candidates: 1 }

test("stage constraints intersect the published schema and cannot widen resource limits", () => {
  const original = JSON.stringify(definitions)
  const projected = projection.projectTools(tools, "science_" + profile.entry, view({
    resource_budget: { properties: { gpu_count: { const: 0 }, max_runtime_minutes: { maximum: 60 } } },
  }))
  const schema = (projected[research].inputSchema as { jsonSchema: JSONSchema7 }).jsonSchema
  expect(accepts(schema, { action: "plan_md_short_qc", resource_budget: budget })).toBe(true)
  expect(accepts(schema, { action: "plan_md_short_qc", resource_budget: { ...budget, gpu_count: 1 } })).toBe(false)
  expect(accepts(schema, { action: "plan_md_short_qc", resource_budget: { ...budget, max_runtime_minutes: 120 } })).toBe(false)
  const widened = projection.projectTools(tools, "science_" + profile.entry, view({ resource_budget: {} }))
  const broad = (widened[research].inputSchema as { jsonSchema: JSONSchema7 }).jsonSchema
  expect(accepts(broad, { action: "plan_md_short_qc", resource_budget: { ...budget, gpu_count: 99 } })).toBe(false)
  expect(accepts(broad, { action: "plan_md_short_qc", resource_budget: { ...budget, cpu_count: -1 } })).toBe(false)
  expect(JSON.stringify(definitions)).toBe(original)
})

test("constraints cannot inject unavailable or host-owned fields", () => {
  for (const name of ["action", "workflow_id", "expected_revision", "request_id", "arbitrary_path"]) {
    expect(() => projection.projectTools(tools, "science_" + profile.entry, view({ [name]: {} }))).toThrow()
  }
})


test("backend views reject constraints on identity before persistence", () => {
  const valid = view({ resource_budget: { properties: { gpu_count: { const: 0 } } } })
  expect(projection.workflowView({ structuredContent: { route: profile.route, workflow: valid } })).toBeDefined()
  for (const name of ["action", "workflow_id", "expected_revision", "request_id", "arbitrary_path"]) {
    expect(projection.workflowView({ structuredContent: { route: profile.route, workflow: view({ [name]: {} }) } })).toBeUndefined()
  }
  const legacy = structuredClone(valid)
  delete (legacy.tools.covalent_library_research as { field_constraints?: unknown }).field_constraints
  expect(projection.workflowView({ structuredContent: { route: profile.route, workflow: legacy } })).toBeDefined()
})

test("real Ligand construct-review fields survive host projection without execution authority", () => {
  const generated = Bun.spawnSync([python, "-c", [
    "import json,tempfile", "from pathlib import Path",
    "from covscreen.mcp.ligand_workflow import TOOLS",
    "from covscreen.ligand_workflow import navigate,directory,view",
    "with tempfile.TemporaryDirectory() as temp:",
    " root=Path(temp)",
    " started=navigate(root,'start',request_id='schema-review',target='EGFR',site='C797',taxon=9606)",
    " state=json.loads((directory(root,started['workflow']['workflow_id'])/'state.json').read_text())",
    " state.update(stage='NEEDS_POSE_EVALUATION',design_id='design-'+'a'*24,evidence_id='hyp-'+'b'*24)",
    " print(json.dumps({'definitions':TOOLS,'view':view(state)}))",
  ].join("\n")], { env: { ...process.env, PYTHONPATH: path.join(workspace, "science/src") } })
  if (generated.exitCode !== 0) throw Error(generated.stderr.toString())
  const actual = JSON.parse(generated.stdout.toString())
  const definitions = actual.definitions as { name: string; inputSchema: JSONSchema7 }[]
  const entry = definitions.find(tool => tool.name === "covalent_ligand_workflow")!
  const profile = workflowInterface.parse((entry.inputSchema as Record<string, unknown>)["x-research-workflow"])
  const scoped = createWorkflowProjection(profile, { key: "construct-test", requestSuffix: "test" })
  const tools = Object.fromEntries(definitions.map(tool => ["science_" + tool.name,
    dynamicTool({ inputSchema: jsonSchema(tool.inputSchema), execute: async () => { throw Error("Schema-only test") } })]))
  const projected = scoped.projectTools(tools, "science_" + profile.entry, actual.view)
  const schema = (projected.science_covalent_ligand_design_step.inputSchema as { jsonSchema: JSONSchema7 }).jsonSchema
  const review = { evidence_sha256: "a".repeat(64), decision: "evidence_insufficient",
    rationale: "The current evidence does not establish suitability.",
    limitations: ["No scientific acceptance is claimed."], segments: [] }
  const selection = { evidence_sha256: "b".repeat(64), choices: { "751": "A", "831": "B" },
    rationale: "Explicit software fixture", limitations: "No suitability claim" }
  expect(accepts(schema, { action: "select_receptor_conformers", conformer_selection: selection })).toBe(true)
  expect(accepts(schema, { action: "select_receptor_conformers",
    conformer_selection: { ...selection, limitations: ["No suitability claim", "B remains unevaluated"] } })).toBe(true)
  expect(accepts(schema, { action: "select_receptor_conformers", conformer_selection: selection, human_approved: true })).toBe(false)
  expect(accepts(schema, { action: "select_receptor_conformers", conformer_selection: { ...selection, choices: { "751": 1 } } })).toBe(false)
  expect(accepts(schema, { action: "read_construct_evidence" })).toBe(true)
  expect(accepts(schema, { action: "review_construct", construct_review: review })).toBe(true)
  expect(accepts(schema, { action: "review_construct", construct_review: review, human_approved: true })).toBe(false)
  expect(accepts(schema, { action: "review_construct", construct_review: { ...review, trim_residues: true } })).toBe(false)
})

test("long tool descriptions are trimmed to overview and stage-permitted actions", () => {
  const longDescription = [
    "Overview sentence one for the workflow tool.",
    "Overview sentence two with global constraints.",
    "resolve_target fetches UniProt identity for the bound target.",
    "plan_md_equilibration freezes the inactive 500 ps NPT protocol.",
    "start_md_short_qc launches short MD integration checks.",
  ].join(" ") + " " + "x".repeat(800)
  const projection = createWorkflowProjection(profile, { key: "desc-trim-test", requestSuffix: "test" })
  const tools = {
    ["science_" + profile.entry]: dynamicTool({ description: "Entry", inputSchema: jsonSchema({
      type: "object", properties: { action: { type: "string" }, request_id: { type: "string" },
        workflow_id: { type: "string" }, expected_revision: { type: "integer" } },
      "x-research-workflow": profile,
    } as JSONSchema7), execute: async () => { throw Error("Schema-only test") } }),
    science_covalent_library_research: dynamicTool({ description: longDescription, inputSchema: jsonSchema({
      type: "object", properties: { action: { type: "string" }, workflow_id: { type: "string" }, expected_revision: { type: "integer" } },
    }), execute: async () => { throw Error("Schema-only test") } }),
  }
  const projected = projection.projectTools(tools, "science_" + profile.entry, {
    protocol: profile.protocol,
    workflow_id: "library-flow-" + "a".repeat(24),
    revision: 1,
    stage: "TARGET_REVIEW",
    tools: {
      covalent_library_research: { actions: ["resolve_target"], fields: ["action"] },
    },
  })
  const desc = projected.science_covalent_library_research.description ?? ""
  expect(desc).toContain("Overview sentence one")
  expect(desc).toContain("resolve_target fetches UniProt identity")
  expect(desc).not.toContain("plan_md_equilibration")
  expect(desc).not.toContain("start_md_short_qc")
})
