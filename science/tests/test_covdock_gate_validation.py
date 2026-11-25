"""Deterministic gate tests; tabular fixtures are not structural evidence."""
import numpy as np
import pandas as pd
import pytest
import json
import runpy
from pathlib import Path

from covscreen.covdock_gate import assess_covdock_candidates, gate_covdock_candidates, merge_pose_consistency


def rows(**changes):
    return pd.DataFrame([{**{"input_sdf_index": 1, "compound_id": "fixture", "engine_status": "SUCCESS",
        "raw_score": -7., "covalent_link_present": True, "covalent_bond_length_angstrom": 1.82,
        "severe_clash": False, "r_strain_Local_Strain_Energy": 4., "af3_covdock_ligand_rmsd_angstrom": 1.5}, **changes}])


@pytest.mark.parametrize("score", [float("inf"), float("-inf"), float("nan"), "invalid"])
def test_nonfinite_engine_scores_fail_and_remain_in_failure_table(tmp_path, score):
    result = gate_covdock_candidates(rows(raw_score=score), tmp_path, min_success_rate=1.)
    assert result["status"] == "quality_gate_failed"
    assert result["quality"]["covdock_success_count"] == 0
    assert result["quality"]["input_identity_count"] == 1
    failures = pd.read_parquet(tmp_path / "stage5_failures.parquet")
    assert failures["compound_id"].tolist() == ["fixture"]
    assert failures.iloc[0]["physical_validity"] == "FAIL"
    assert failures.iloc[0]["covdock_gate"] == "C"
    assert pd.read_parquet(tmp_path / "final_candidates.parquet").empty


@pytest.mark.parametrize("column", ["covalent_link_present", "covalent_bond_length_angstrom", "severe_clash", "r_strain_Local_Strain_Energy"])
def test_missing_physical_measurements_are_unknown_not_pass(column):
    source = rows().drop(columns=[column])
    result = assess_covdock_candidates(source).iloc[0]
    assert result["physical_validity"] == "UNKNOWN"
    assert not result["physical_evidence_complete"]
    assert result["covdock_gate_detail"] == "B2"


def test_missing_consensus_does_not_erase_complete_physical_measurements():
    result = assess_covdock_candidates(rows(af3_covdock_ligand_rmsd_angstrom=np.nan)).iloc[0]
    assert result["physical_validity"] == "PASS"
    assert result["physical_evidence_complete"]
    assert result["cross_method_consensus"] == "NA"
    assert result["covdock_gate_detail"] == "B2"


@pytest.mark.parametrize("rmsd", [-1., float("inf"), float("-inf")])
def test_invalid_rmsd_cannot_be_high_consensus(rmsd):
    result = assess_covdock_candidates(rows(af3_covdock_ligand_rmsd_angstrom=rmsd)).iloc[0]
    assert result["cross_method_consensus"] == "NA"
    assert "pose_consistency_invalid" in result["covdock_gate_reason"]
    assert result["covdock_gate_detail"] == "B2"


def test_strain_fallback_uses_first_finite_available_value():
    source = rows(r_strain_Local_Strain_Energy=np.inf, r_strain_Strain_Energy=5.)
    result = assess_covdock_candidates(source).iloc[0]
    assert result["local_strain_energy"] == 5.
    assert result["covdock_gate_detail"] == "A"


@pytest.mark.parametrize("parameters", [
    {"max_local_strain": np.inf}, {"hard_max_strain": np.nan}, {"max_pose_rmsd": -1},
    {"max_covalent_bond_length": 1.}, {"min_covalent_bond_length": 0},
    {"hard_max_strain": 2.}, {"max_local_strain": None}, {"min_success_rate": 1.1},
    {"min_success_rate": np.nan}, {"min_success_rate": True}, {"final_n": 1.5}, {"final_n": True},
])
def test_invalid_policy_is_rejected_before_creating_outputs(tmp_path, parameters):
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        gate_covdock_candidates(rows(), output, **parameters)
    assert not output.exists()


@pytest.mark.parametrize("index", [1.5, -1, np.inf, np.nan, True, 2**63])
def test_identity_mapping_never_truncates_or_wraps_indices(index):
    with pytest.raises(ValueError, match="integer indices"):
        assess_covdock_candidates(rows(input_sdf_index=index))
    with pytest.raises(ValueError, match="integer indices"):
        merge_pose_consistency(rows(), pd.DataFrame({"input_sdf_index": [index], "af3_covdock_ligand_rmsd_angstrom": [1.]}))


def test_readonly_assessment_matches_written_gates_and_preserves_input(tmp_path):
    source = pd.concat([rows(), rows(input_sdf_index=2, compound_id="missing", severe_clash=None)], ignore_index=True)
    original = source.copy(deep=True)
    assessed = assess_covdock_candidates(source)
    assert list(tmp_path.iterdir()) == []
    pd.testing.assert_frame_equal(source, original)
    manifest = gate_covdock_candidates(source, tmp_path, min_success_rate=1.)
    written = pd.read_parquet(tmp_path / "stage5_gated_candidates.parquet")
    pd.testing.assert_frame_equal(assessed.sort_values("input_sdf_index").reset_index(drop=True),
                                  written.sort_values("input_sdf_index").reset_index(drop=True))
    assert manifest["quality"]["physical_unknown_count"] == 1
    assert manifest["algorithm_version"] == "covdock-lo-pose-gate-v2.1"


def test_real_pose_parser_cannot_select_negative_infinity_over_finite_score(tmp_path):
    parse = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/parse_covdock_scores.py"))["parse_inspection"]
    identity = tmp_path / "identity.parquet"
    pd.DataFrame({"input_sdf_index": [1, 2], "compound_id": ["finite", "invalid"]}).to_parquet(identity, index=False)
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"input": str(tmp_path / "fixture.maegz"), "rows": [
        {"title": "finite__i1", "pose_index": 0, "score": float("-inf")},
        {"title": "finite__i1", "pose_index": 1, "score": -8.},
        {"title": "invalid__i2", "pose_index": 2, "score": float("inf")},
    ]}))
    result = parse(audit, identity, "score", "lower_is_better").set_index("compound_id")
    assert result.loc["finite", "raw_score"] == -8.
    assert result.loc["finite", "covdock_pose_index"] == 1
    assert result.loc["invalid", "engine_status"] == "FAILED"
    assert result.loc["invalid", "failure_reason"] == "missing_pose_or_finite_score"
