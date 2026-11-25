import { test, expect } from "bun:test"
import { mkdtemp, mkdir, rm, unlink, symlink } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createStudy, importHistorical, readImport, studyOverview, registeredStudySource } from "./study-store"
import { studyRequest, inputReadiness, studyRoutes } from "@synsci/util/discovery-study"
import { routeDefaults } from "@synsci/util/discovery-config"
import { result as discoveryResult } from "@synsci/util/discovery-result"

const request = () => ({ title: "Interface acceptance fixture", target: { id: "test-target", gene: "TEST",
  taxon: 9606, uniprot: "TEST", site: { chain: "B", residue: "CYS", number: 42, atom: "SG", numbering: "canonical" },
  receptor: { reference: "test-receptor" } }, hypothesis: { state: "pending", rationale: "Test only", evidence: [] },
  routes: Object.fromEntries(studyRoutes.map(route => [route, { selected: true, mode: "reuse", inputs: {} }])) })

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "discovery-study-"))
  await mkdir(path.join(root, ".discovery"))
  await Bun.write(path.join(root, ".discovery/project.json"), JSON.stringify({ schema: "discovery-project-v1", project: "test-project",
    target: { id: "test-target", name: "TEST", label: "TEST C42",
      identity: { gene: "TEST", taxon: 9606, uniprot: "TEST", numbering: "canonical" },
      site: { chain: "B", residue: "CYS", number: 42, atom: "SG" } },
    routes: Object.fromEntries(studyRoutes.map(route => [route, { ...routeDefaults[route], dataPath: route + ".json" }])) }))
  for (const route of studyRoutes) await Bun.write(path.join(root, route + ".json"), JSON.stringify({
    schema: "discovery-result-v1", project: "test-project", target: "test-target", route, run: "old-" + route,
    collectedAt: "2026-01-01T00:00:00.000Z", evidenceUpdatedAt: "2025-01-01T00:00:00.000Z", observation: "local-evidence",
    current: "", notice: "Fixture, not science", stages: [], stats: [], candidates: [], charts: [], reports: [],
    sources: [{ path: "fixture", sha256: "a".repeat(64), updated: "2025-01-01T00:00:00.000Z" }],
    analysis: { state: "waiting", expectedReplicas: 0, observedReplicas: 0, message: "No MD" } }))
  return root
}

test("fresh IDs, three real ingress calls, frozen history, no computation", async () => {
  const root = await fixture()
  try {
    const first = await createStudy(root, "test-project", request())
    const second = await createStudy(root, "test-project", request())
    expect(first.id).not.toBe(second.id)
    for (const route of studyRoutes) {
      const receipt = await importHistorical(root, "test-project", first.id, route)
      expect(receipt.mode).toBe("historical-reuse")
      expect(receipt.newComputeSubmitted).toBe(false)
      expect(receipt.source.evidenceUpdatedAt).toBe("2025-01-01T00:00:00.000Z")
      expect(await importHistorical(root, "test-project", first.id, route)).toEqual(receipt)
    }
    expect((await studyOverview(root, "test-project", first.id)).routes.every(r => r.state === "historical-imported")).toBe(true)
    expect((await studyOverview(root, "test-project", second.id)).routes.every(r => r.state === "awaiting-import")).toBe(true)
    const receipt = (await readImport(root, "test-project", first.id, "de-novo")).receipt
    await Bun.write(path.join(root, receipt.snapshot), "tampered")
    await expect(readImport(root, "test-project", first.id, "de-novo")).rejects.toThrow("snapshot changed")
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("registered source is discoverable without mutating a new study or crossing project scope", async () => {
  const root = await fixture()
  try {
    const source = await registeredStudySource(root, "test-project")
    expect(source?.target?.id).toBe("test-target")
    expect(source?.routes).toEqual([...studyRoutes])
    await expect(registeredStudySource(root, "other-project")).rejects.toThrow("project mismatch")
    const input = request()
    input.target.id = "another-target"
    const plan = await createStudy(root, "test-project", input)
    expect(plan.target.id).toBe("another-target")
    expect((await studyOverview(root, "test-project", plan.id)).candidates).toEqual([])
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("all three ingress routes preserve values and expose missing evaluation evidence", async () => {
  const root = await fixture()
  try {
    const plan = await createStudy(root, "test-project", request())
    for (const route of studyRoutes) {
      const filename = path.join(root, route + ".json")
      const data = await Bun.file(filename).json()
      data.candidates = [{ id: "candidate-1", label: "Fixture", smiles: "CC", featured: false,
        badge: "source label", tone: "gray", note: "", structures: [],
        metrics: [{ id: "reported", label: "Reported", value: 0, unit: "test", definition: "Fixture only" },
          { id: "unproven", label: "Unproven", value: 1, unit: "test", definition: "No provenance" },
          { id: "unknown", label: "Unknown", value: null, unit: "test", definition: "Not available" }],
        md: { state: "pending", summary: "Not run", independentReplicas: 0, metrics: [], chartIds: [] } }]
      data.candidates[0].scientificEvidence = {
        schema: "candidate-scientific-evidence-v1", candidateID: "candidate-1", smiles: "CC",
        ...(route === "de-novo" ? { pose: { state: "reported", representation: "prereaction",
          molecule: { path: "fixture.sdf", sha256: "b".repeat(64) }, recordIndex: 0,
          method: "engineering-fixture", evidence: { path: "fixture.json", sha256: "c".repeat(64) },
          coordinateBinding: "not_independently_verified" } } : {}),
        measurements: [{ metricID: "reported", scope: "candidate", stage: "chemical",
          method: "engineering-fixture", definition: "Fixture only", unit: "test",
          inputs: [{ path: "fixture.sdf", sha256: "b".repeat(64) }],
          evidence: { path: "fixture.json", sha256: "c".repeat(64) } }],
      }
      const changed = structuredClone(data)
      changed.candidates[0].scientificEvidence.candidateID = "another-candidate"
      expect(discoveryResult.safeParse(changed).success).toBe(false)
      changed.candidates[0].scientificEvidence.candidateID = "candidate-1"
      changed.candidates[0].scientificEvidence.measurements[0].unit = "different-unit"
      expect(discoveryResult.safeParse(changed).success).toBe(false)
      await Bun.write(filename, JSON.stringify(data))
      await importHistorical(root, "test-project", plan.id, route)
    }
    const view = await studyOverview(root, "test-project", plan.id, false)
    expect(view.candidates).toHaveLength(3)
    for (const source of view.candidates) {
      const handoff = source.handoff!
      expect(handoff.provenance.route).toBe(source.route)
      expect(handoff.provenance.sha256).toBe(source.sourceSha256)
      expect(handoff.metrics.map(item => item.state)).toEqual(["reported_value", "reported_value", "missing"])
      expect(handoff.metrics[0].metric.value).toBe(0)
      expect(handoff.metrics[0].computationProvenance).toHaveProperty("method", "engineering-fixture")
      expect(handoff.metrics[1].computationProvenance).toHaveProperty("state", "missing")
      expect(handoff.missingEvidence).toContain("metric_computation_provenance:candidate:unproven")
      expect(handoff.missingEvidence).toContain("metric_computation_provenance")
      expect(handoff.missingEvidence).toContain("metric_provenance_verification")
      expect(handoff.metrics[0].interpretation?.state).toBe("unregistered")
      expect(handoff.sourceEvidence?.candidateID).toBe(source.candidate.id)
      expect(handoff.warhead.reactionAtoms.state).toBe("missing")
      expect(handoff.target.numberingMap.state).toBe("missing")
      expect(handoff.poses).toEqual([])
      expect(handoff.decision).toBe("not_assessed")
      if (source.route === "de-novo") {
        expect(handoff.poseProvenance).toMatchObject({
          state: "reported", coordinateBinding: "not_independently_verified",
          molecule: { path: "fixture.sdf", sha256: "b".repeat(64) },
        })
        expect(handoff.missingEvidence).toContain("pose_target_binding")
        expect(handoff.missingEvidence).not.toContain("pose")
      } else {
        expect(handoff.poseProvenance.state).toBe("missing")
        expect(handoff.missingEvidence).toContain("pose")
      }
    }
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("different target never falls back to registered target", async () => {
  const root = await fixture()
  try {
    const input = request()
    input.target.id = "other-target"
    input.target.gene = "OTHER"
    input.target.site.number = 909
    const plan = await createStudy(root, "test-project", input)
    expect(plan.target.site.number).toBe(909)
    for (const route of studyRoutes) await expect(importHistorical(root, "test-project", plan.id, route)).rejects.toThrow("target does not match")
    await expect(studyOverview(root, "another-project", plan.id)).rejects.toThrow("identity mismatch")
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("input declarations are not execution readiness", () => {
  const value = studyRequest.parse(request())
  value.routes["ligand-first"].mode = "new"
  expect(inputReadiness(value, "ligand-first").state).toBe("missing-inputs")
  value.routes["ligand-first"].inputs = { parent: "test-parent", parentBinding: "noncovalent", warhead: "acrylamide" }
  expect(inputReadiness(value, "ligand-first").missing).toEqual(["parentStructure", "receptorStructure"])
  value.routes["ligand-first"].inputs.parentStructure = "parent.sdf"
  value.routes["ligand-first"].inputs.receptorStructure = "receptor.pdb"
  expect(inputReadiness(value, "ligand-first").state).toBe("awaiting-adapter-validation")
  value.routes["library-first"].mode = "new"
  value.routes["library-first"].inputs = { library: "library.sdf", warhead: "acrylamide" }
  expect(inputReadiness(value, "library-first").missing).toEqual(["screening_scale_and_budget"])
  value.routes["de-novo"].mode = "new"
  value.routes["de-novo"].inputs = { pocket: "pocket.pdb", warhead: "acrylamide" }
  expect(inputReadiness(value, "de-novo").missing).toEqual(["generation_slots_and_seed"])
  value.routes["de-novo"].selected = false
  expect(inputReadiness(value, "de-novo").state).toBe("not-selected")
})

test("a single selected route is normalized without inventing hashes or enabling other routes", () => {
  const input = request()
  const selected = studyRequest.parse({ ...input, routes: {
    "de-novo": { selected: true, mode: "new", inputs: { pocket: "explicit", warhead: "acrylamide" },
      generation: { slots: 1, seed: 123 } },
  } })
  expect(selected.routes["library-first"].selected).toBe(false)
  expect(selected.routes["ligand-first"].selected).toBe(false)
  expect(selected.target.receptor.sha256).toBeUndefined()
  expect(selected.routes["de-novo"].generation).toEqual({ slots: 1, seed: 123 })
  expect(studyRequest.safeParse({ ...input, routes: {} }).success).toBe(false)
  expect(studyRequest.safeParse({ ...input,
    target: { ...input.target, receptor: { reference: "explicit", sha256: "" } } }).success).toBe(false)
})

test("historical ingress rejects mismatched biological identity and freezes accepted identity", async () => {
  const root = await fixture()
  try {
    for (const update of [{ gene: "OTHER" }, { taxon: 10090 }, { uniprot: "OTHER" },
      { site: { ...request().target.site, numbering: "structure" } }]) {
      const input = request()
      Object.assign(input.target, update)
      const plan = await createStudy(root, "test-project", input)
      for (const route of studyRoutes)
        await expect(importHistorical(root, "test-project", plan.id, route)).rejects.toThrow("species/UniProt/numbering")
      expect((await studyOverview(root, "test-project", plan.id)).candidates).toEqual([])
    }
    const plan = await createStudy(root, "test-project", request())
    const saved = await importHistorical(root, "test-project", plan.id, "library-first")
    expect(saved.targetIdentity).toEqual({ gene: "TEST", taxon: 9606, uniprot: "TEST", site: plan.target.site })
    const config = await Bun.file(path.join(root, ".discovery/project.json")).json()
    delete config.target.identity
    await Bun.write(path.join(root, ".discovery/project.json"), JSON.stringify(config))
    expect((await readImport(root, "test-project", plan.id, "library-first")).receipt).toEqual(saved)
    const next = await createStudy(root, "test-project", request())
    await expect(importHistorical(root, "test-project", next.id, "library-first")).rejects.toThrow("needs registered species")
    const filename = path.join(root, ".discovery/studies", plan.id, "plan.json")
    await Bun.write(filename, JSON.stringify({ ...plan, target: { ...plan.target, taxon: 10090 } }))
    await expect(readImport(root, "test-project", plan.id, "library-first")).rejects.toThrow("Frozen import target identity")
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("repeat import uses frozen evidence even after current route source is removed", async () => {
  const root = await fixture()
  try {
    const plan = await createStudy(root, "test-project", request())
    const receipt = await importHistorical(root, "test-project", plan.id, "library-first")
    await unlink(path.join(root, "library-first.json"))
    expect(await importHistorical(root, "test-project", plan.id, "library-first")).toEqual(receipt)
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("same target label cannot hide a changed reactive site", async () => {
  const root = await fixture()
  try {
    const config = await Bun.file(path.join(root, ".discovery/project.json")).json()
    config.target.site = { chain: "B", residue: "CYS", number: 43, atom: "SG" }
    await Bun.write(path.join(root, ".discovery/project.json"), JSON.stringify(config))
    const plan = await createStudy(root, "test-project", request())
    await expect(importHistorical(root, "test-project", plan.id, "library-first")).rejects.toThrow("site does not match")
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("plan and receipt symlinks cannot read outside the project", async () => {
  const root = await fixture()
  const outside = await mkdtemp(path.join(os.tmpdir(), "study-external-"))
  try {
    const plan = await createStudy(root, "test-project", request())
    const receipt = await importHistorical(root, "test-project", plan.id, "library-first")
    const filename = path.join(root, ".discovery/studies", plan.id, "library-first.import.json")
    await Bun.write(path.join(outside, "receipt.json"), JSON.stringify(receipt))
    await unlink(filename)
    await symlink(path.join(outside, "receipt.json"), filename)
    await expect(readImport(root, "test-project", plan.id, "library-first")).rejects.toThrow("escapes project")
    const source = path.join(root, ".discovery/studies", plan.id, "plan.json")
    await Bun.write(path.join(outside, "plan.json"), JSON.stringify(plan))
    await unlink(source)
    await symlink(path.join(outside, "plan.json"), source)
    await expect(studyOverview(root, "test-project", plan.id)).rejects.toThrow("escapes project")
  } finally {
    await rm(root, { recursive: true, force: true })
    await rm(outside, { recursive: true, force: true })
  }
})
