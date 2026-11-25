#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

from covscreen.ligand_first_geometry import (
    LigandFirstGeometryPolicy,
    evaluate_carsidock_geometry,
    load_sdf_poses,
)
from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a real CarsiDock-Cov Ligand-first ensemble.")
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--candidate-json", type=Path, required=True)
    parser.add_argument("--raw-poses", type=Path, required=True)
    parser.add_argument("--no-dummy-poses", type=Path, required=True)
    parser.add_argument("--carsidock-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    campaign_path = args.campaign.resolve()
    campaign = load_campaign(campaign_path)
    parent_pose = build_parent_pose_mol(campaign, campaign_path.parent)
    candidate_payload = json.loads(args.candidate_json.read_text())
    candidates = candidate_payload.get("candidates", [])
    if len(candidates) != 1:
        raise SystemExit(f"V1 evaluator expects one candidate, found {len(candidates)}")
    result = evaluate_carsidock_geometry(
        campaign=campaign,
        parent_pose=parent_pose,
        candidate_id=candidates[0]["candidate_id"],
        carsidock_run_id=args.carsidock_run_id,
        raw_poses=load_sdf_poses(args.raw_poses),
        no_dummy_poses=load_sdf_poses(args.no_dummy_poses),
        receptor_pdb=campaign_path.parent / campaign.parent_pose.structure_path,
        policy=LigandFirstGeometryPolicy(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
