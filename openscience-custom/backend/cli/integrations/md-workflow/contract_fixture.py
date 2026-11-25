"""Software-only subprocess fixture. NO molecular calculation or cloud calls."""
import argparse
import hashlib
import json
from pathlib import Path
import time


def sha(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


parser = argparse.ArgumentParser()
for field in ("plan", "stage", "output", "upstream"):
    parser.add_argument("--" + field, required=True)
args = parser.parse_args()
planfile = Path(args.plan)
plan = json.loads(planfile.read_text())
assert plan["evidence_scope"] == "software_test", "Fixture can never supply scientific results"
output = Path(args.output)
mode = plan.get("fixture", "")
if mode == "timeout" and args.stage == "prepare":
    time.sleep(30)
if mode == "nonzero" and args.stage == "short_qc":
    raise SystemExit(3)
(output / "software-test.txt").write_text("Test contract only, NOT a trajectory or scientific result.\n")
all_checks = (
    "sequence_provenance missing_residues_resolved chemistry_identity "
    "atom_mapping bonded_coverage nonbonded_coverage charge_accounting covalent_state_matches_plan conversion_audit "
    "reactant_or_adduct_graph bond_orders nucleophile_state formal_charge stereochemistry no_severe_clash "
    "protein_local_structure pocket_retention anchor_interactions unrestrained_relaxation cpu_cuda_energy_force_agreement "
    "all_frames_read finite_coordinates time_axis periodic_box input_identity reaction_atom_mapping gating_definition_frozen"
).split()
steps = [step["name"] for step in plan["stages"]]
metrics = ["protein_rmsd", "ligand_rmsd", "ligand_com", "pocket_contacts", "hydrogen_bonds", "physical_qc"]
metrics += ["nac", "gating"] if plan["phase"] == "pre_covalent" else ["covalent_geometry"]
if mode == "adduct_gating":
    metrics += ["nac", "gating"]
evidence = {
    "schema": "md-stage-evidence-v1", "run_id": plan["run_id"], "plan_sha256": sha(planfile),
    "phase": plan["phase"], "stage": args.stage, "evidence_scope": "software_test", "status": "PASS",
    "upstream_evidence_sha256": {name: sha(Path(args.upstream) / name / "evidence.json")
                                for name in steps[:steps.index(args.stage)]},
    "checks": {name: True for name in all_checks}, "files": {"software-test.txt": sha(output / "software-test.txt")},
    "forcefield": plan["forcefield"], "permanent_reactive_geometry_restraint": False,
    "reactive_geometry_observation": "lost", "production_ps": plan["production_ps"],
    "frames": round(plan["production_ps"] / plan["frame_interval_ps"]), "unrestrained_production": True,
    "metrics": metrics, "affinity_or_kinact_proven": False, "scientific_review": "pending",
    "roles": {role: "software-test.txt" for role in ("report", "tables", "figures")},
}
if mode == "bad_chemistry":
    evidence["checks"]["bond_orders"] = False
if mode == "missing_loop":
    evidence["checks"]["missing_residues_resolved"] = False
if mode == "incomplete":
    evidence["frames"] -= 1
if mode == "wrong_run":
    evidence["run_id"] = "another-run"
if mode == "wrong_upstream" and args.stage == "analysis":
    evidence["upstream_evidence_sha256"] = {}
if mode == "fake_science":
    evidence["evidence_scope"] = "scientific"
if mode == "escape":
    evidence["files"] = {"../software-test.txt": "0" * 64}
(output / "evidence.json").write_text(json.dumps(evidence))
