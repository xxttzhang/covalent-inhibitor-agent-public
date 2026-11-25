import { test, expect } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm } from "node:fs/promises"
import { studyRequest } from "@synsci/util/discovery-study"
import { warheadRegistry } from "@synsci/util/warhead-registry"
import { createStudy, readStudy } from "./study-store"
import { branchStudy, materializeDeNovoBranch, deNovoBranches, deNovoCapability } from "./study-denovo-branches"
import { compileStudyDeNovo } from "./study-denovo-compiler"
const workspace = path.resolve(import.meta.dir, "../../../../..")
const profile = path.join(workspace, "routes/de-novo/registry/egfr_c797_v1")
function request() {
  return { title: "Engineering fixture, not scientific evidence", target: {
    id: "egfr", gene: "EGFR", taxon: 9606, uniprot: "P00533",
    site: { chain: "A", residue: "CYS", number: 797, atom: "SG", numbering: "canonical" },
    receptor: { reference: "PDB:4G5J" } }, hypothesis: { state: "pending", rationale: "Test", evidence: [] },
    routes: { "de-novo": { selected: true, mode: "new", inputs: {} } },
    denovoBranches: warheadRegistry.entries.map((entry, i) => ({ id: entry.id, warhead: entry.id,
      pocket: "registered:egfr-c797-acrylamide-v1", generation: { slots: 2, seed: i },
      rationale: "Test", evidence: [{ reference: "fixture", interpretation: "hypothesis", statement: "Test only" }] })) }
}
async function fixture(run: (root: string, parent: Awaited<ReturnType<typeof createStudy>>) => Promise<void>) {
  const root = await mkdtemp(path.join(os.tmpdir(), "multi-warhead-"))
  try { await run(root, await createStudy(root, "test", request())) }
  finally { await rm(root, { recursive: true, force: true }) }
}
test("declarations reject duplicate IDs, conflated chemistry and missing evidence", () => {
  const value = request()
  for (const warhead of ["cyanoacrylamide", "methyl cyanoacrylate", "unknown"])
    expect(studyRequest.safeParse({ ...value, denovoBranches: [{ ...value.denovoBranches[0], warhead }] }).success).toBe(false)
  expect(studyRequest.safeParse({ ...value, denovoBranches: [value.denovoBranches[0], value.denovoBranches[0]] }).success).toBe(false)
  expect(studyRequest.safeParse({ ...value, denovoBranches: [{ ...value.denovoBranches[0], evidence: [] }] }).success).toBe(false)
})
test("four independent child studies and concurrent replay preserve identities without compute", () => fixture(async (root, parent) => {
  const children = await Promise.all(parent.denovoBranches!.map(branch => materializeDeNovoBranch(root, "test", parent.id, branch.id)))
  const replay = await Promise.all([1, 2, 3].map(() => materializeDeNovoBranch(root, "test", parent.id, "nitrile")))
  expect(new Set(children.map(child => child.study_id)).size).toBe(4)
  expect(new Set(replay.map(child => child.study_id))).toEqual(new Set([children[2]!.study_id]))
  const child = await readStudy(root, "test", children[2]!.study_id)
  expect(child.denovoOrigin?.parent).toBe(parent.id)
  expect(child.hypothesis.state).toBe("pending")
  expect(child.denovoBranches).toBeUndefined()
  expect(children.every(child => child.compute_submitted === false)).toBe(true)
  await expect(materializeDeNovoBranch(root, "other", parent.id, "nitrile")).rejects.toThrow()
}))
test("unsupported branches never borrow acrylamide execution", () => fixture(async (_root, parent) => {
  for (const id of ["nitrile", "cyanamide", "methyl-acrylate"]) {
    const child = branchStudy(parent, id)
    expect((await deNovoCapability(child, profile)).state).toBe("WAITING_CAPABILITY")
    await expect(compileStudyDeNovo(child, "{}", { directory: workspace, session: "test", package: profile, worker: "unused" })).rejects.toThrow("WAITING_CAPABILITY")
  }
  await expect(compileStudyDeNovo(parent, "{}", { directory: workspace, session: "test", package: profile, worker: "unused" })).rejects.toThrow("individual child")
}))
test("summary distinguishes uncreated and evidence-pending branches without ranking", () => fixture(async (root, parent) => {
  const before = await deNovoBranches(root, "test", parent.id)
  expect(before.branches.every(branch => branch.stage === "NOT_MATERIALIZED")).toBe(true)
  await materializeDeNovoBranch(root, "test", parent.id, "nitrile")
  const after = await deNovoBranches(root, "test", parent.id)
  const row = after.branches.find(branch => branch.branch_id === "nitrile")!
  expect(row.stage).toBe("EVIDENCE_REQUIRED")
  expect(row.capability.state).toBe("WAITING_CAPABILITY")
  expect(row.candidates).toEqual([])
  expect(after.budget.requested_generation_slots).toBe(8)
  expect(after.comparison.ranked).toBe(false)
}))
test("tampered child is rejected and isolated from sibling summaries", () => fixture(async (root, parent) => {
  const child = await materializeDeNovoBranch(root, "test", parent.id, "nitrile")
  const filename = path.join(root, ".discovery/studies", child.study_id, "plan.json")
  const value = await Bun.file(filename).json(); value.routes["de-novo"].inputs.warhead = "acrylamide"
  await Bun.write(filename, JSON.stringify(value))
  await expect(readStudy(root, "test", child.study_id)).rejects.toThrow("differs")
  const summary = await deNovoBranches(root, "test", parent.id)
  expect(summary.branches.find(branch => branch.branch_id === "nitrile")!.stage).toBe("NEEDS_REVIEW")
  expect(summary.branches.find(branch => branch.branch_id === "cyanamide")!.stage).toBe("NOT_MATERIALIZED")
}))
test("explicit site override cannot silently bind the registered package", () => fixture(async (_root, parent) => {
  parent.denovoBranches![0]!.target = { ...parent.target, site: { ...parent.target.site, number: 790 } }
  const child = branchStudy(parent, "acrylamide")
  expect(child.target.site.number).toBe(790)
  expect((await deNovoCapability(child, profile)).state).toBe("WAITING_CAPABILITY")
}))

test("session replay includes branch declarations and rejects replacing the campaign", () => fixture(async (root, parent) => {
  const { bindStudySession, createSessionStudy } = await import("./study-session")
  const { Storage } = await import("../../src/storage/storage")
  const scope = { directory: root, project: "test", session: "ses_" + crypto.randomUUID().replaceAll("-", "") }
  try {
    await bindStudySession(scope, parent.id)
    expect((await createSessionStudy(scope, request())).reused).toBe(true)
    const changed = request(); changed.denovoBranches[0]!.generation.seed = 99
    await expect(createSessionStudy(scope, changed)).rejects.toThrow("already has study")
  } finally { await Storage.remove(["study-session-binding", scope.project, scope.session]) }
}))
