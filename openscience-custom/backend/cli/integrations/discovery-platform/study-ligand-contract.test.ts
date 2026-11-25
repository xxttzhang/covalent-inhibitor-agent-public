import { expect, test } from "bun:test"
import path from "node:path"
import { studyLigandContract } from "./study-ligand-contract"

test("Ligand contract compares actual campaign mapping and rejects changed study inputs", async () => {
  const root = path.resolve(import.meta.dir, "../../../../..")
  const plan = await Bun.file(path.join(root, "openscience-custom/.discovery/studies/study-00d8e67f-169a-4453-9e47-07ded41a738f/plan.json")).json()
  const campaign = await Bun.file(path.join(root, "science/projects/egfr-c797/ligand_first/campaign.json")).json()
  plan.routes["ligand-first"] = { selected: true, mode: "new", inputs: { parent: "erlotinib", parentBinding: "noncovalent", warhead: "acrylamide", parentStructure: "parent.sdf", receptorStructure: "protein.pdb" } }
  // Contract fixture with real campaign metadata; not a real compiled science run.
  const value = { hypothesis: { study_id: plan.id, route: "ligand-first", parent_ligand: "erlotinib", parent_binding_mode: "noncovalent", parent_evidence_ids: ["PDB:1M17"] },
    campaign_registration: { historical_runs_reused: false, input_binding: { target: campaign.target, parent_pose: campaign.parent_pose,
      paths: { pdb: "protein.pdb", reference_ligand: "parent.sdf", campaign_path: "fresh.json" }, project_id: "fresh",
      allowed_warheads: campaign.allowed_warheads, seed_run_ids: {} } },
    handoff: { compute_submitted: false, route_plan: { submitted: false, chain: { next_stage: "structure_intake", stages: [{ state: "NOT_SUBMITTED", run_id: null, parent_run_id: null }] },
      next_request: { project_id: "fresh", execution_mode: "plan", human_approved: false, parameters: { campaign: "fresh.json" } } } } }
  expect(studyLigandContract(plan, JSON.stringify(value)).state).toBe("UPSTREAM_PLAN_BINDING_VALIDATED")
  for (const key of ["parent", "parentStructure", "receptorStructure", "warhead"]) {
    const changed = structuredClone(plan)
    changed.routes["ligand-first"].inputs[key] = "different"
    expect(studyLigandContract(changed, JSON.stringify(value)).blockers.length).toBeGreaterThan(0)
  }
  for (const key of ["chain", "number"]) {
    const changed = structuredClone(plan)
    changed.target.site[key] = key === "number" ? 481 : "B"
    expect(studyLigandContract(changed, JSON.stringify(value)).blockers.length).toBeGreaterThan(0)
  }
  const seeded = structuredClone(value)
  seeded.campaign_registration.input_binding.seed_run_ids = { af3: "old" }
  expect(studyLigandContract(plan, JSON.stringify(seeded)).blockers.length).toBeGreaterThan(0)
  expect(studyLigandContract(plan, JSON.stringify(value)).compiled).toBe(false)
  for (const state of ["RUNNING", "COMPLETED", "FAILED", "QUEUED"]) {
    const stale = structuredClone(value)
    stale.handoff.route_plan.chain.stages[0].state = state
    expect(studyLigandContract(plan, JSON.stringify(stale)).state).toBe("NEEDS_INPUTS_OR_ADAPTER")
  }
  const empty = structuredClone(value)
  empty.handoff.route_plan.chain.stages = []
  expect(studyLigandContract(plan, JSON.stringify(empty)).state).toBe("NEEDS_INPUTS_OR_ADAPTER")
})

test("real frozen upstream Ligand plan passes fresh-state validation", async () => {
  const root = path.resolve(import.meta.dir, "../../../../..", "openscience-custom/.discovery/studies/study-c92778ff-f0a4-4743-bf95-ea403b8966ac")
  const plan = await Bun.file(path.join(root, "plan.json")).json()
  const { readHypothesis } = await import("./study-hypothesis")
  const hypothesis = await readHypothesis(path.resolve(import.meta.dir, "../../../.."), plan, "ligand-first")
  expect(hypothesis).not.toBeNull()
  expect(studyLigandContract(plan, hypothesis!.original).state).toBe("UPSTREAM_PLAN_BINDING_VALIDATED")
})
