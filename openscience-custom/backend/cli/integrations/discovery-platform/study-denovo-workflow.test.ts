import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm, readdir } from "node:fs/promises"
import { createStudy } from "./study-store"
import { deNovoWorkflow } from "./study-denovo-workflow"
import { compileDeNovoPlan } from "./study-denovo-plan"

test("workflow restores the same real study without inventing a plan or executing", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "denovo-workflow-"))
  try {
    const original = { schema: "discovery-study-v1", id: "study-00000000-0000-4000-8000-000000000001", project: "test", createdAt: "2026-01-01T00:00:00Z",
      title: "Engineering De novo fixture", target: { id: "egfr-test", gene: "EGFR", taxon: 9606, uniprot: "P00533",
        receptor: { reference: "PDB:4G5J" }, site: { chain: "A", residue: "CYS", number: 797, atom: "SG", numbering: "canonical" } },
      hypothesis: { state: "pending", rationale: "Engineering fixture, not scientific evidence", evidence: [] },
      routes: { "library-first": { selected: false, mode: "new", inputs: {} },
        "ligand-first": { selected: false, mode: "new", inputs: {} },
        "de-novo": { selected: true, mode: "new", inputs: {}, generation: { slots: 1, seed: 321 } } } }
    const { schema, id, project, createdAt, ...request } = original
    request.routes["library-first"].selected = false
    request.routes["de-novo"] = { selected: true, mode: "new", inputs: { pocket: "explicit", warhead: "acrylamide" }, generation: { slots: 1, seed: 12 } }
    const study = await createStudy(root, "test", request)
    const first = await deNovoWorkflow(root, "test", study.id)
    expect(first.stage).toBe("EVIDENCE_REQUIRED")
    expect(first.target).toEqual(study.target)
    expect(first.generation).toEqual({ slots: 1, seed: 12 })
    expect(first.execution.state).toBe("not_submitted")
    expect(first.assessment.state).toBe("not_evaluated")
    expect(first.compute_submitted_by_this_call).toBe(false)
    expect(first.task_id).toBeNull()
    expect(first.next).toEqual({ tool: "discovery_study", action: "start-reasoning", study_id: study.id })
    expect(await deNovoWorkflow(root, "test", study.id)).toEqual(first)
    await expect(compileDeNovoPlan(root, study)).rejects.toThrow("Attach this study")
    expect(await readdir(path.join(root, ".discovery/studies", study.id))).toEqual(["plan.json"])
    await expect(deNovoWorkflow(root, "another-project", study.id)).rejects.toThrow("identity mismatch")
    const disabled = await createStudy(root, "test", { ...request,
      routes: { ...request.routes, "de-novo": { ...request.routes["de-novo"], selected: false },
        "library-first": { ...request.routes["library-first"], selected: true } } })
    await expect(deNovoWorkflow(root, "test", disabled.id)).rejects.toThrow("Select a new")
  } finally { await rm(root, { recursive: true, force: true }) }
})
