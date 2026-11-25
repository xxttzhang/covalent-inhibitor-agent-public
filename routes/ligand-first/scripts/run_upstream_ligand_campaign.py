"""Registered campaign adapter for native Covalentizer enumeration.

Uses the full parent as input (no new attachment planner or fragment heuristic).
Native upstream rules supply their own attachment sites and short linkers.
"""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
from rdkit import Chem

from covscreen.jobs import sha256_file
from covscreen.ligand_first_inputs import load_campaign, audit_ligand_first_inputs, build_parent_pose_mol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--upstream-repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-candidates", type=int, required=True)
    args = parser.parse_args()
    if args.max_candidates < 1:
        parser.error("candidate budget must be positive")
    campaign = load_campaign(args.campaign.resolve())
    audit, _ = audit_ligand_first_inputs(campaign, args.campaign.resolve().parent)
    if audit.status != "PASS":
        raise ValueError(f"campaign input audit failed: {audit.errors}")
    parent = build_parent_pose_mol(campaign, args.campaign.resolve().parent)
    for atom in parent.GetAtoms():
        atom.SetAtomMapNum(0)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "input_audit.json").write_text(audit.model_dump_json(indent=2) + "\n")
    source = out / "upstream_parent.smi"
    source.write_text(Chem.MolToSmiles(parent) + "\t" + campaign.parent_pose.ligand_component_id + "\n")
    command = [sys.executable, str(Path(__file__).with_name("run_upstream_covalentizer_enumeration.py")),
               "--upstream-repo", str(args.upstream_repo.resolve()),
               "--fragment-smiles", str(source), "--parent-smiles-file", str(source),
               "--min-parent-fraction", "1.0", "--output-dir", str(out),
               "--export-docking-inputs", "--target-id", campaign.target.symbol,
               "--site-id", f"{campaign.target.symbol}_C{campaign.target.nucleophile.uniprot_residue_number}",
               "--parent-id", campaign.parent_pose.ligand_component_id]
    subprocess.run(command, check=True)
    handoff = json.loads((out / "docking_inputs/upstream_handoff_manifest.json").read_text())
    accepted = int(handoff["accepted_count"])
    budget_pass = accepted <= args.max_candidates
    payload = {
        "schema_version": "1.0", "campaign_id": campaign.campaign_id,
        "planning_mode": campaign.planning_mode,
        "parent_retention_policy": "chirality-aware full-parent graph, minimum fraction 1.0",
        "candidate_interaction_retention_status": "NOT_ASSESSED",
        "input_audit_sha256": sha256_file(out / "input_audit.json"),
        "site_discovery_mode": "upstream-covalentizer-native-v1",
        "generated_count": handoff["generated_count"], "chemistry_pass_count": accepted,
        "status": "prepared" if budget_pass and accepted else "budget_or_chemistry_gate_failed",
        "quality_gates": {"input_audit": True, "parent_mapping_required": True,
                          "nonempty_candidates": accepted > 0, "candidate_budget": budget_pass},
        "candidate_budget": args.max_candidates,
        "upstream_manifest_sha256": sha256_file(out / "upstream_enumeration_manifest.json"),
        "handoff_manifest_sha256": sha256_file(out / "docking_inputs/upstream_handoff_manifest.json"),
        "scope": "native full-parent enumeration; no branch-replacement or new planner",
        "limitations": ["Full parent graph retention does not establish preserved binding interactions.",
                        "Native sulfur family is vinyl sulfonamide, not vinyl sulfone.",
                        "No docking or scoring has been performed by this stage."],
    }
    (out / "ligand_first_candidates.json").write_text(json.dumps(payload, indent=2) + "\n")
    (out / "ligand_first_failures.json").write_text(json.dumps(handoff["failures"], indent=2) + "\n")
    if not budget_pass or not accepted:
        return 4
    # Standard stage artifact names allow the unchanged CarsiDock stage to
    # consume this generator. Raw upstream intermediates stay in the same run.
    table = pd.read_csv(out / "docking_inputs/ligand_first_candidates.csv")
    table.to_parquet(out / "ligand_first_candidates.parquet", index=False)
    table.to_csv(out / "ligand_first_candidates.csv", index=False)
    (out / "ligand_first_candidates.sdf").write_bytes(
        (out / "docking_inputs/ligand_first_candidates.sdf").read_bytes())
    print(json.dumps({"status": "prepared", "candidate_count": accepted}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
