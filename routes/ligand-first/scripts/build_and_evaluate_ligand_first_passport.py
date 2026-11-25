#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

from rdkit import Chem

from covscreen.ligand_first_geometry import LigandFirstGeometryResult, load_sdf_poses
from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign
from covscreen.ligand_first_passport import (
    artifact_evidence,
    build_unified_candidate_passport,
    independently_evaluate_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a unified Candidate Passport and run the independent Ligand-first evaluator."
    )
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--candidate-json", type=Path, required=True)
    parser.add_argument("--candidate-sdf", type=Path, required=True)
    parser.add_argument("--generation-failures", type=Path, required=True)
    parser.add_argument("--geometry-json", type=Path, required=True)
    parser.add_argument("--raw-poses", type=Path, required=True)
    parser.add_argument("--no-dummy-poses", type=Path, required=True)
    parser.add_argument("--carsidock-manifest", type=Path, required=True)
    parser.add_argument("--carsidock-run-id", required=True)
    parser.add_argument("--output-passport", type=Path, required=True)
    parser.add_argument("--output-evaluation", type=Path, required=True)
    args = parser.parse_args()

    root = args.project_root.resolve()
    campaign_path = args.campaign.resolve()
    campaign = load_campaign(campaign_path)
    parent_pose = build_parent_pose_mol(campaign, campaign_path.parent)
    candidate_payload = json.loads(args.candidate_json.read_text())
    if len(candidate_payload.get("candidates", [])) != 1:
        raise SystemExit("V1 Passport builder requires exactly one candidate")
    candidate_record = candidate_payload["candidates"][0]
    candidate_molecules = [
        mol
        for mol in Chem.SDMolSupplier(str(args.candidate_sdf), removeHs=False, sanitize=True)
        if mol is not None
    ]
    if len(candidate_molecules) != 1:
        raise SystemExit(f"expected one candidate SDF molecule, found {len(candidate_molecules)}")
    geometry = LigandFirstGeometryResult.model_validate_json(args.geometry_json.read_text())
    independent = independently_evaluate_candidate(
        campaign=campaign,
        campaign_dir=campaign_path.parent,
        parent_pose=parent_pose,
        candidate_record=candidate_record,
        candidate_mol=candidate_molecules[0],
        saved_geometry=geometry,
        expected_carsidock_run_id=args.carsidock_run_id,
        raw_poses=load_sdf_poses(args.raw_poses),
        no_dummy_poses=load_sdf_poses(args.no_dummy_poses),
        receptor_pdb=campaign_path.parent / campaign.parent_pose.structure_path,
    )
    failures = json.loads(args.generation_failures.read_text()).get("failures", [])
    failures.extend(
        [
            {
                "run_id": "20260817T134517-carsidock-cov-9cd5955616",
                "stage": "carsidock-runtime",
                "failure_type": "CXXABI_RUNTIME_ORDER",
                "failure_reason": "First attempt failed before docking and was retained; worker-local runtime ordering was fixed before linked retry.",
            }
        ]
    )
    artifact_specs = [
        (campaign_path, "campaign_manifest"),
        (args.candidate_json, "candidate_table"),
        (args.candidate_sdf, "candidate_structure"),
        (args.generation_failures, "failure_ledger"),
        (args.geometry_json, "geometry_evaluation"),
        (args.raw_poses, "covalent_pose_ensemble"),
        (args.no_dummy_poses, "no_dummy_pose_ensemble"),
        (args.carsidock_manifest, "carsidock_run_manifest"),
    ]
    artifacts = [artifact_evidence(root, path.resolve(), role) for path, role in artifact_specs]
    passport = build_unified_candidate_passport(
        campaign=campaign,
        candidate_record=candidate_record,
        geometry=geometry,
        independent=independent,
        artifacts=artifacts,
        failure_ledger=failures,
    )
    args.output_evaluation.parent.mkdir(parents=True, exist_ok=True)
    args.output_passport.parent.mkdir(parents=True, exist_ok=True)
    args.output_evaluation.write_text(
        json.dumps(independent.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    )
    args.output_passport.write_text(
        json.dumps(passport.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "passport_id": passport.passport_id,
                "candidate_id": passport.candidate_id,
                "independent_evaluation": independent.status,
                "decision": independent.decision,
                "next_stage_handoffs": independent.next_stage_handoffs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if independent.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
