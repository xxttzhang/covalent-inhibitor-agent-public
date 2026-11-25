import { test, expect } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { studyActionResult } from "./study-actions"

test("new BTK study preserves parameters and exposes unsupported separately from missing and disabled", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "study-admission-"))
  try {
    const result = await studyActionResult(root, "admission-test", { action: "create", request: {
      title: "BTK interface test, no computation", target: { id: "btk-c481", gene: "BTK", uniprot: "Q06187", taxon: 9606,
        site: { chain: "A", residue: "CYS", number: 481, atom: "SG", numbering: "canonical" },
        receptor: { reference: "PDB:5P9J" } }, hypothesis: { state: "pending", rationale: "", evidence: [] },
      routes: {
        "library-first": { selected: true, mode: "new", inputs: { library: "new/library.sdf", warhead: "acrylamide" },
          planning: { screeningMode: "economy", maxPoints: 1 } },
        "ligand-first": { selected: true, mode: "new", inputs: {} },
        "de-novo": { selected: false, mode: "new", inputs: {} },
      },
    } })
    if (!result || !("plan" in result) || !result.plan || !("id" in result.plan)) throw Error("Missing study")
    const view = await studyActionResult(root, "admission-test", { action: "get", study_id: result.plan.id })
    if (!("routes" in view)) throw Error("Missing overview")
    expect(view.routes.map(route => route.state)).toEqual(["unsupported", "missing-inputs", "not-selected"])
    expect(view.plan.target.gene).toBe("BTK")
    expect(view.plan.target.site.number).toBe(481)
    expect(view.plan.target.receptor.reference).toBe("PDB:5P9J")
    expect(view.plan.routes["library-first"].inputs.library).toBe("new/library.sdf")
    expect(view.candidates).toEqual([])
    expect(view.libraryPlan).toBeNull()
    expect(view.newComputeSubmitted).toBe(false)
    const routes = structuredClone(view.plan.routes)
    routes["de-novo"] = { selected: true, mode: "new",
      inputs: { pocket: "btk/pocket.pdb", warhead: "acrylamide" }, generation: { slots: 1, seed: 17 } }
    const next = await studyActionResult(root, "admission-test", { action: "create", request: {
      title: "BTK generation admission", target: view.plan.target, hypothesis: view.plan.hypothesis, routes,
    } })
    if (!next || !("plan" in next) || !next.plan || !("id" in next.plan)) throw Error("Missing study")
    const checked = await studyActionResult(root, "admission-test", { action: "get", study_id: next.plan.id })
    if (!("routes" in checked)) throw Error("Missing overview")
    expect(checked.routes.find(route => route.route === "de-novo")?.state).toBe("unsupported")
    expect(checked.plan.routes["de-novo"].inputs.pocket).toBe("btk/pocket.pdb")
    expect(checked.denovoPlan).toBeNull()
    expect(checked.newComputeSubmitted).toBe(false)
  } finally { await rm(root, { recursive: true, force: true }) }
})
