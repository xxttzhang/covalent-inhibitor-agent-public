import json
from pathlib import Path

from rdkit import Chem

from covscreen.ligand_first_geometry import LigandFirstGeometryResult, load_sdf_poses
from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign
from covscreen.ligand_first_passport import independently_evaluate_candidate


ROOT = (Path(__file__).resolve().parents[3] / "science")
BASE = ROOT / "projects/egfr-c797/ligand_first"
CAMPAIGN_PATH = BASE / "campaign.json"
GEOMETRY_DIR = BASE / "geometry/carsidock_20260817T135020"


def _inputs():
    campaign = load_campaign(CAMPAIGN_PATH)
    candidate_record = json.loads((BASE / "generated/candidates.json").read_text())["candidates"][0]
    candidate_mol = next(
        mol
        for mol in Chem.SDMolSupplier(str(BASE / "generated/candidates.sdf"), removeHs=False)
        if mol is not None
    )
    geometry = LigandFirstGeometryResult.model_validate_json(
        (BASE / "geometry/geometry_evaluation.json").read_text()
    )
    return campaign, candidate_record, candidate_mol, geometry


def test_independent_evaluator_recomputes_chemistry_and_geometry_before_advance():
    campaign, record, molecule, geometry = _inputs()
    result = independently_evaluate_candidate(
        campaign=campaign,
        campaign_dir=CAMPAIGN_PATH.parent,
        parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_record=record,
        candidate_mol=molecule,
        saved_geometry=geometry,
        expected_carsidock_run_id="20260817T135020-carsidock-cov-be42799293",
        raw_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf"),
        no_dummy_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf"),
        receptor_pdb=CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path,
    )
    assert result.status == "PASS", result.failure_reasons
    assert result.decision == "ADVANCE"
    assert all(result.verified_checks.values())
    assert result.recomputed_chemistry_gate.status == "PASS"
    assert result.recomputed_geometry.passing_pose_count == 6
    assert result.next_stage_handoffs["md_evaluation_agent"] == "READY"
    assert result.next_stage_handoffs["experimental_validation"] == "NOT_RUN"


def test_independent_evaluator_holds_on_cross_artifact_run_mismatch():
    campaign, record, molecule, geometry = _inputs()
    tampered = geometry.model_copy(update={"carsidock_run_id": "tampered-run-id"})
    result = independently_evaluate_candidate(
        campaign=campaign,
        campaign_dir=CAMPAIGN_PATH.parent,
        parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_record=record,
        candidate_mol=molecule,
        saved_geometry=tampered,
        expected_carsidock_run_id="20260817T135020-carsidock-cov-be42799293",
        raw_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf"),
        no_dummy_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf"),
        receptor_pdb=CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path,
    )
    assert result.status == "FAIL"
    assert result.decision == "HOLD"
    assert result.verified_checks["geometry_artifact_consistent"] is False
