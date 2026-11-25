import { afterAll, expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, mkdir, rm } from "node:fs/promises"
import { createHash, randomBytes } from "node:crypto"
import { study } from "@synsci/util/discovery-study"
import { compileStudyDeNovo, validateWarheadDecision } from "./study-denovo-compiler"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const evidenceRoot = await mkdtemp(path.join(os.tmpdir(), "denovo-evidence-"))
afterAll(() => rm(evidenceRoot, { recursive: true, force: true }))
const sha = (text: string) => createHash("sha256").update(text).digest("hex")
const profile = await Bun.file(path.join(workspace, "routes/de-novo/registry/egfr_c797_v1/profile.json")).json()
const environment = { directory: profile.allowed_directories[0], session: "ses_compile_only",
  evidenceRoot,
  package: path.join(workspace, "routes/de-novo/registry/egfr_c797_v1"),
  worker: path.join(workspace, "routes/de-novo/runtime/worker.py") }
async function fixture() {
  const raw = study.parse({ schema: "discovery-study-v1", id: "study-00000000-0000-4000-8000-000000000001", project: "test", createdAt: "2026-01-01T00:00:00Z",
      title: "Engineering De novo fixture", target: { id: "egfr-test", gene: "EGFR", taxon: 9606, uniprot: "P00533",
        receptor: { reference: "PDB:4G5J" }, site: { chain: "A", residue: "CYS", number: 797, atom: "SG", numbering: "canonical" } },
      hypothesis: { state: "pending", rationale: "Engineering fixture, not scientific evidence", evidence: [] },
      routes: { "library-first": { selected: false, mode: "new", inputs: {} },
        "ligand-first": { selected: false, mode: "new", inputs: {} },
        "de-novo": { selected: true, mode: "new", inputs: {}, generation: { slots: 1, seed: 321 } } } })
  raw.target.receptor.reference = "PDB:4G5J"
  raw.routes["library-first"].selected = false
  raw.routes["de-novo"] = { selected: true, mode: "new", inputs: { pocket: "registered:egfr-c797-acrylamide-v1", warhead: "acrylamide" }, generation: { slots: 1, seed: 321 } }
  const plan = study.parse(raw)
  // Engineering hypothesis fixture, not a saved scientific result.
  const evidenceID = "hyp-" + randomBytes(12).toString("hex")
  const folder = path.join(evidenceRoot, "results/target-hypotheses", evidenceID)
  await mkdir(folder, { recursive: true })
  const evidence = JSON.stringify({ target: "EGFR", site: "C797", taxon: 9606,
    identity: { uniprot_id: "P00533", canonical_cys_verified: true }, literature: [{ id: "PMID:test" }] })
  const manifest = JSON.stringify({ files: { "evidence.json": sha(evidence) } })
  await Bun.write(path.join(folder, "evidence.json"), evidence)
  await Bun.write(path.join(folder, "manifest.json"), manifest)
  const proposal = { evidence_id: evidenceID, evidence_manifest_sha256: sha(manifest),
    references: [{ id: "PMID:test" }],
    hypothesis: { study_id: plan.id, route: "de-novo", warhead_class: ["acrylamide"],
      warhead_selection: { selected_warhead: "acrylamide", statement: "Engineering test only",
        evidence_ids: ["PMID:test"], interpretation: "inference", alternatives: [],
        no_alternatives_reason: "No alternative evidence in engineering fixture" } },
    target: { symbol: "EGFR", site: "C797", taxon: 9606, uniprot_id: "P00533" } }
  return { plan, proposal }
}
test("study compiles actual generator request and hashes, with no invented user approval origin", async () => {
  const { plan, proposal } = await fixture()
  const first = await compileStudyDeNovo(plan, JSON.stringify(proposal), environment)
  plan.routes["de-novo"].generation = { slots: 2, seed: 322 }
  const second = await compileStudyDeNovo(plan, JSON.stringify(proposal), environment)
  expect(first.request.slots).toBe(1)
  expect(second.request.slots).toBe(2)
  expect(second.request.seed).toBe(322)
  expect(first.request.id).not.toBe(second.request.id)
  expect(first.request.user_message_id).toBeUndefined()
  expect(first.inputHashes.receptor).toMatch(/^[a-f0-9]{64}$/)
  expect(first.inputHashes.pocket).toMatch(/^[a-f0-9]{64}$/)
  expect(first.submitted).toBe(false)
  expect(first.request.scientific_basis.execution_branch_id).toBe("primary")
  expect(first.request.scientific_basis.hypothesis_sha256).toMatch(/^[a-f0-9]{64}$/)
})
test("changed target, pocket, chain, structure and omitted generation cannot use historical defaults", async () => {
  for (const change of ["target", "pocket", "chain", "structure", "generation"]) {
    const { plan, proposal } = await fixture()
    if (change === "target") { plan.target.gene = "BTK"; proposal.target.symbol = "BTK" }
    if (change === "pocket") plan.routes["de-novo"].inputs.pocket = "other-pocket.json"
    if (change === "chain") plan.target.site.chain = "B"
    if (change === "structure") plan.target.receptor.reference = "PDB:1M17"
    if (change === "generation") delete plan.routes["de-novo"].generation
    await expect(compileStudyDeNovo(plan, JSON.stringify(proposal), environment)).rejects.toThrow()
  }
})

test("residue code casing is equivalent without changing the frozen study or relaxing site identity", async () => {
  const { plan, proposal } = await fixture()
  for (const residue of ["Cys", "cys", "CYS"]) {
    plan.target.site.residue = residue
    const before = JSON.stringify(plan)
    expect((await compileStudyDeNovo(plan, JSON.stringify(proposal), environment)).compiled).toBe(true)
    expect(JSON.stringify(plan)).toBe(before)
  }
  plan.target.site.residue = "SER"
  await expect(compileStudyDeNovo(plan, JSON.stringify(proposal), environment)).rejects.toThrow("no matching")
  plan.target.site.residue = "Cys"
  plan.target.site.chain = "a"
  await expect(compileStudyDeNovo(plan, JSON.stringify(proposal), environment)).rejects.toThrow("no matching")
})

test("unresolved, unknown citations and missing rationale cannot compile an execution decision", async () => {
  const { proposal } = await fixture()
  const decision = proposal.hypothesis.warhead_selection
  for (const changed of [undefined, { ...decision, status: "unresolved", selected_warhead: null, execution_branch_id: null },
    { ...decision, evidence_ids: ["invented"] }, { ...decision, no_alternatives_reason: undefined }]) {
    expect(() => validateWarheadDecision(changed, proposal.references, ["acrylamide"])).toThrow()
  }
})
test("a research branch requires explicit selection and matching execution scope", async () => {
  const { proposal } = await fixture()
  const decision = { ...proposal.hypothesis.warhead_selection, branches: [{ branch_id: "second",
    warhead: "propiolamide", statement: "Engineering branch only", evidence_ids: ["PMID:test"], interpretation: "inference" }] }
  expect(validateWarheadDecision(decision, proposal.references, ["acrylamide"]).execution_branch_id).toBe("primary")
  expect(() => validateWarheadDecision({ ...decision, execution_branch_id: "second" }, proposal.references, ["acrylamide"])).toThrow()
  expect(validateWarheadDecision({ ...decision, execution_branch_id: "second" }, proposal.references, ["propiolamide"]).execution_branch_id).toBe("second")
})

test("modified sealed evidence and forged references cannot compile", async () => {
  const { plan, proposal } = await fixture()
  const file = path.join(evidenceRoot, "results/target-hypotheses", proposal.evidence_id, "evidence.json")
  await Bun.write(file, "{}")
  await expect(compileStudyDeNovo(plan, JSON.stringify(proposal), environment)).rejects.toThrow("Sealed evidence file changed")
  const second = await fixture()
  second.proposal.references.push({ id: "fabricated" })
  await expect(compileStudyDeNovo(second.plan, JSON.stringify(second.proposal), environment)).rejects.toThrow("absent from sealed evidence")
})

test("native study binding preserves the frozen scientific plan and never grants approval", async () => {
  const { bindStudyDeNovoRequest } = await import("../../src/tool/denovo-study")
  const { plan, proposal } = await fixture()
  const compiled = await compileStudyDeNovo(plan, JSON.stringify(proposal), environment)
  const input = { ...environment, session: "ses_execute", message: "msg_explicit_execute", text: `执行 de-novo 研究 ${plan.id}` }
  const bound = await bindStudyDeNovoRequest(compiled, plan.id, input)
  expect(bound.id).toBe(compiled.request.id)
  expect(bound.slots).toBe(compiled.request.slots)
  expect(bound.seed).toBe(compiled.request.seed)
  expect(bound.scientific_basis).toEqual(compiled.request.scientific_basis)
  expect(bound.session_id).toBe("ses_execute")
  expect(bound.planning_session_id).toBe(environment.session)
  expect(bound).not.toHaveProperty("approved")
  expect(bound).not.toHaveProperty("authorization")
  const pending = await bindStudyDeNovoRequest(compiled, plan.id, { ...input, text: "Generate candidates for my bound study" }, true)
  expect(pending.id).toBe(bound.id)
  expect(pending.user_text).toBe("Generate candidates for my bound study")
  expect(pending).not.toHaveProperty("approved")
  expect(pending).not.toHaveProperty("authorization")
  await expect(bindStudyDeNovoRequest(compiled, plan.id, { ...input, text: "" }, true)).rejects.toThrow("real user")
  await expect(bindStudyDeNovoRequest(compiled, plan.id, { ...input, text: input.text + "，改为96个" })).rejects.toThrow("exact direct")
  await expect(bindStudyDeNovoRequest({ ...compiled, approved: true }, plan.id, input)).rejects.toThrow()
  await expect(bindStudyDeNovoRequest({ ...compiled, request: { ...compiled.request, worker_sha256: "0".repeat(64) } }, plan.id, input)).rejects.toThrow("code differs")
})

test("native binding rejects changed evidence, citations, branch and unresolved decisions before compute", async () => {
  const { bindStudyDeNovoRequest } = await import("../../src/tool/denovo-study")
  const { plan, proposal } = await fixture()
  const compiled = await compileStudyDeNovo(plan, JSON.stringify(proposal), environment)
  const input = { ...environment, session: "ses_execute", message: "msg_execute", text: `执行 de-novo 研究 ${plan.id}` }
  for (const mutation of ["manifest", "evidence", "identity", "branch", "citation", "unresolved"]) {
    const changed = structuredClone(compiled)
    const basis = changed.request.scientific_basis
    if (mutation === "manifest") basis.manifest_json = "{}"
    if (mutation === "evidence") basis.evidence_json = "{}"
    if (mutation === "identity") basis.evidence_id = "hyp-" + "0".repeat(24)
    if (mutation === "branch") basis.execution_branch_id = "invented"
    if (mutation === "citation" || mutation === "unresolved") {
      const content = JSON.parse(basis.hypothesis_json)
      if (mutation === "citation") content.references.push({ id: "fabricated" })
      if (mutation === "unresolved") content.hypothesis.warhead_selection.status = "unresolved"
      basis.hypothesis_json = JSON.stringify(content)
      basis.hypothesis_sha256 = sha(basis.hypothesis_json)
    }
    await expect(bindStudyDeNovoRequest(changed, plan.id, input)).rejects.toThrow()
  }
})

test("materialized acrylamide branch compiles with its own frozen provenance and cannot inherit parent evidence identity", async () => {
  const { plan, proposal } = await fixture()
  const { branchStudy } = await import("./study-denovo-branches")
  const parent = study.parse({ ...plan, denovoBranches: [{ id: "acrylamide", warhead: "acrylamide",
    pocket: plan.routes["de-novo"].inputs.pocket, generation: { slots: 2, seed: 71 }, rationale: "Engineering fixture",
    evidence: [{ reference: "PMID:test", interpretation: "hypothesis", statement: "Fixture only" }] }] })
  const child = branchStudy(parent, "acrylamide")
  await expect(compileStudyDeNovo(child, JSON.stringify(proposal), environment)).rejects.toThrow()
  proposal.hypothesis.study_id = child.id
  const compiled = await compileStudyDeNovo(child, JSON.stringify(proposal), environment)
  expect(compiled.request.scientific_basis.branch_origin).toEqual(child.denovoOrigin)
  expect(compiled.study).toBe(child.id)
  expect(compiled.request.slots).toBe(2)
  expect(compiled.request.seed).toBe(71)
  expect(compiled.approved).toBe(false)
})
