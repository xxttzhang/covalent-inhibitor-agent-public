from pathlib import Path

import pandas as pd
import pytest

from covscreen.ligand_first_evaluation import independently_evaluate_ligand_first_batch, write_ligand_first_evaluation
from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign
from covscreen.rdkit_covalentizer import covalentize_parent


ROOT = (Path(__file__).resolve().parents[3] / "science")
CAMPAIGN = ROOT / "projects/egfr-c797/ligand_first/campaign.json"


def _candidate_row() -> dict:
    campaign = load_campaign(CAMPAIGN)
    parent = build_parent_pose_mol(campaign, CAMPAIGN.parent)
    candidate, _ = covalentize_parent(
        parent,
        campaign,
        "quinazoline-c6-primary",
        "replace_aryl_ether_with_acrylamide",
    )
    return {
        "input_sdf_index": 0,
        "compound_id": candidate.candidate_id,
        "standardized_smiles": candidate.canonical_smiles,
        "warhead_class": candidate.warhead_name,
        "af3_mpae": 2.0,
        "covdock_success": True,
        "covalent_link_verified": True,
        "severe_clash_verified": False,
        "covalent_bond_length_angstrom": 1.82,
        "local_strain_energy": 5.0,
    }


def test_chemistry_recomputed_but_upstream_physics_is_not_independent_pass():
    evaluated = independently_evaluate_ligand_first_batch(
        pd.DataFrame([_candidate_row()]), CAMPAIGN
    )
    row = evaluated.iloc[0]
    assert row.independent_gate == "WARN"
    assert row.independent_chemistry_valid
    assert row.independent_warhead_match_count == 1
    assert row.independent_parent_mcs_fraction >= 0.60
    assert not bool(row.high_reactivity_alert)
    assert not bool(row.aggregation_risk)
    assert row.pains_alert == ""
    assert "RDKit_PAINS_first_match" in row.risk_annotation_method
    assert row.physical_validity == "WARN"
    assert not row.independent_geometry_evaluated


def test_generic_score_cannot_substitute_for_af3_mpae():
    candidate = _candidate_row()
    candidate.pop("af3_mpae")
    candidate["raw_score"] = 2.0
    evaluated = independently_evaluate_ligand_first_batch(pd.DataFrame([candidate]), CAMPAIGN)
    assert "af3_mpae_missing" in evaluated.iloc[0].independent_failure_reasons
    assert evaluated.iloc[0].independent_gate == "FAIL"


def test_coordinate_recheck_overrides_optimistic_upstream_geometry():
    evidence = pd.DataFrame([{
        "compound_id": _candidate_row()["compound_id"],
        "input_sdf_index": 0, "coordinate_recheck_status": "SUCCESS",
        "coordinate_recheck_link": True, "coordinate_recheck_clash": True,
        "coordinate_recheck_bond_length": 1.8,
    }])
    result = independently_evaluate_ligand_first_batch(
        pd.DataFrame([_candidate_row()]), CAMPAIGN, coordinate_evidence=evidence)
    assert result.iloc[0].independent_geometry_evaluated
    assert result.iloc[0].independent_gate == "FAIL"
    assert "severe_nonbonded_clash" in result.iloc[0].independent_failure_reasons


def test_candidate_cannot_self_assert_independent_coordinate_check():
    candidate = _candidate_row()
    candidate["coordinate_recheck_status"] = "SUCCESS"
    candidate["independent_geometry_evaluated"] = True
    result = independently_evaluate_ligand_first_batch(pd.DataFrame([candidate]), CAMPAIGN)
    assert not result.iloc[0].independent_geometry_evaluated
    assert result.iloc[0].independent_gate == "WARN"


def test_independent_batch_rejects_missing_af3_and_broken_covalent_link():
    row = _candidate_row()
    row["af3_mpae"] = None
    row["covalent_link_verified"] = False
    evaluated = independently_evaluate_ligand_first_batch(
        pd.DataFrame([row]), CAMPAIGN
    )
    assert evaluated.iloc[0].independent_gate == "FAIL"
    assert "af3_mpae_missing" in evaluated.iloc[0].independent_failure_reasons
    assert "specified_covalent_link_missing" in evaluated.iloc[0].independent_failure_reasons


def test_sulfone_and_sulfonamide_are_not_interchangeable():
    rows = []
    for index, (smiles, label) in enumerate([
        ("C=CS(=O)(=O)Nc1ccccc1", "vinyl_sulfonamide"),
        ("C=CS(=O)(=O)c1ccccc1", "vinyl_sulfone"),
        ("C=CS(=O)(=O)Nc1ccccc1", "vinyl_sulfone"),
    ]):
        row = _candidate_row()
        row.update(input_sdf_index=index, compound_id=f"sulfur_probe_{index}",
                   standardized_smiles=smiles, warhead_class=label)
        rows.append(row)
    # This fixture isolates warhead classification, not parent scaffold quality.
    evaluated = independently_evaluate_ligand_first_batch(pd.DataFrame(rows), CAMPAIGN,
                                                          min_parent_mcs_fraction=0)
    assert list(evaluated.independent_warhead_match_count) == [1, 1, 0]
    assert list(evaluated.independent_total_supported_warhead_count) == [1, 1, 1]
    assert list(evaluated.independent_chemistry_valid) == [True, True, False]


def test_undeclared_second_warhead_fails_independent_chemistry():
    row = _candidate_row()
    row.update(standardized_smiles="C=CC(=O)NCCNC(=O)CCl", warhead_class="acrylamide")
    evaluated = independently_evaluate_ligand_first_batch(pd.DataFrame([row]), CAMPAIGN,
                                                          min_parent_mcs_fraction=0)
    assert evaluated.iloc[0].independent_warhead_match_count == 1
    assert evaluated.iloc[0].independent_total_supported_warhead_count == 2
    assert not evaluated.iloc[0].independent_chemistry_valid
    assert "total_supported_warhead_count_not_one" in evaluated.iloc[0].independent_failure_reasons


def test_manifest_rejects_lost_identity_and_required_missing_coordinates(tmp_path):
    candidates = pd.DataFrame([_candidate_row()])
    source = tmp_path / "input.parquet"
    candidates.to_parquet(source, index=False)
    evaluated = independently_evaluate_ligand_first_batch(candidates, CAMPAIGN)
    manifest = write_ligand_first_evaluation(
        evaluated, tmp_path / "missing_coordinates", input_path=source,
        campaign_path=CAMPAIGN, require_coordinate_recheck=True)
    assert manifest["quality_gates"]["input_denominator_preserved"]
    assert not manifest["quality_gates"]["eligible_candidates_coordinate_rechecked"]
    assert manifest["status"] == "quality_gate_failed"
    evaluated.loc[0, "compound_id"] = "wrong-compound"
    manifest = write_ligand_first_evaluation(
        evaluated, tmp_path / "wrong_identity", input_path=source, campaign_path=CAMPAIGN)
    assert not manifest["quality_gates"]["input_denominator_preserved"]
    assert manifest["status"] == "quality_gate_failed"


def test_manifest_accepts_verified_identity_and_coordinate_evidence(tmp_path):
    candidates = pd.DataFrame([_candidate_row()])
    source = tmp_path / "input.parquet"
    candidates.to_parquet(source, index=False)
    evidence = pd.DataFrame([{
        "compound_id": _candidate_row()["compound_id"],
        "input_sdf_index": 0, "coordinate_recheck_status": "SUCCESS",
        "coordinate_recheck_link": True, "coordinate_recheck_clash": False,
        "coordinate_recheck_bond_length": 1.8,
    }])
    evaluated = independently_evaluate_ligand_first_batch(
        candidates, CAMPAIGN, coordinate_evidence=evidence)
    manifest = write_ligand_first_evaluation(
        evaluated, tmp_path / "verified", input_path=source,
        campaign_path=CAMPAIGN, require_coordinate_recheck=True)
    assert manifest["status"] == "completed"


@pytest.mark.parametrize("wrong_id", [None, "another-compound"])
def test_coordinate_evidence_rejects_same_index_wrong_compound(wrong_id):
    evidence = pd.DataFrame([{"input_sdf_index": 0, "compound_id": wrong_id,
                              "coordinate_recheck_status": "SUCCESS"}])
    with pytest.raises(ValueError, match="compound identity mismatch"):
        independently_evaluate_ligand_first_batch(
            pd.DataFrame([_candidate_row()]), CAMPAIGN, coordinate_evidence=evidence)


def test_coordinate_evidence_cannot_replace_chemistry_or_scores():
    candidate = _candidate_row()
    evidence = pd.DataFrame([{"input_sdf_index": 0,
        "compound_id": candidate["compound_id"], "standardized_smiles": "CC",
        "af3_mpae": -999, "coordinate_recheck_status": "SUCCESS",
        "coordinate_recheck_link": True, "coordinate_recheck_clash": False,
        "coordinate_recheck_bond_length": 1.8}])
    result = independently_evaluate_ligand_first_batch(
        pd.DataFrame([candidate]), CAMPAIGN, coordinate_evidence=evidence)
    assert result.iloc[0].standardized_smiles == candidate["standardized_smiles"]
    assert result.iloc[0].af3_mpae == candidate["af3_mpae"]
