"""Software validation only: synthetic tables are not molecular ground truth."""
import numpy as np
import pandas as pd
import pytest

from covscreen.consensus import fuse_complete_rankings, fuse_rankings
from covscreen.covdock_gate import (
    ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, STRAIN_COLUMNS,
    assess_covdock_candidates, gate_covdock_candidates,
)


DIRECTIONS = {"a_score": "lower_is_better", "b_score": "lower_is_better"}


def covdock_row(**updates):
    return pd.DataFrame([{
        "input_sdf_index": 0, "compound_id": "synthetic", "engine_status": "SUCCESS",
        "raw_score": -7., "r_strain_Local_Strain_Energy": 4.,
        "covalent_link_present": True, "covalent_bond_length_angstrom": 1.82,
        "severe_clash": False, "af3_covdock_ligand_rmsd_angstrom": 1.5,
        **updates,
    }])


def strict(frame):
    return assess_covdock_candidates(frame, algorithm_version=STRICT_ALGORITHM_VERSION)


@pytest.mark.parametrize("bad", [np.inf, -np.inf, np.nan, None, "bad", pd.NA])
def test_partial_and_absent_results_never_receive_fused_rank(bad):
    source = pd.DataFrame({"a_score": [1., bad, bad], "b_score": [1., 2., bad]})
    original = source.copy(deep=True)
    result = fuse_complete_rankings(source, DIRECTIONS)
    assert result.evidence_coverage.tolist() == ["COMPLETE", "PARTIAL", "NONE"]
    assert result.n_engines_success.tolist() == [2, 1, 0]
    assert result.consensus_rank.iloc[0] == 1
    assert result.consensus_rank.iloc[1:].isna().all()
    assert result.consensus_score.iloc[1:].isna().all()
    pd.testing.assert_frame_equal(source, original)


def test_partial_candidates_cannot_change_complete_case_fusion_scores():
    source = pd.DataFrame({"id": ["x", "y"], "a_score": [1., 2.], "b_score": [2., 1.]})
    extra = pd.DataFrame({"id": ["partial"], "a_score": [-100.], "b_score": [None]})
    before = fuse_complete_rankings(source, DIRECTIONS).set_index("id")
    after = fuse_complete_rankings(pd.concat([source, extra], ignore_index=True), DIRECTIONS).set_index("id")
    pd.testing.assert_series_equal(before.consensus_score, after.loc[before.index, "consensus_score"])


def test_ties_and_duplicate_dataframe_indices_are_order_invariant():
    source = pd.DataFrame({"id": ["x", "y", "z"], "a_score": [1., 1., 2.], "b_score": [1., 1., 3.]}, index=[0, 0, 0])
    first = fuse_complete_rankings(source, DIRECTIONS).set_index("id").sort_index()
    second = fuse_complete_rankings(source.iloc[::-1], DIRECTIONS).set_index("id").sort_index()
    pd.testing.assert_frame_equal(first, second)
    assert first.loc["x", "consensus_rank"] == first.loc["y", "consensus_rank"] == 1


def test_empty_frame_is_valid_empty_evidence():
    result = fuse_complete_rankings(pd.DataFrame(columns=DIRECTIONS), DIRECTIONS)
    assert result.empty
    assert "evidence_coverage" in result


@pytest.mark.parametrize("k", [-1, np.inf, np.nan, True, "60"])
def test_invalid_fusion_parameter_rejected(k):
    with pytest.raises(ValueError):
        fuse_complete_rankings(pd.DataFrame({"a_score": [1], "b_score": [2]}), DIRECTIONS, k)


def test_legacy_fusion_is_preserved_and_strict_result_is_versioned():
    source = pd.DataFrame({"a_score": [np.inf, None], "b_score": [1., None]})
    old = fuse_rankings(source, DIRECTIONS)
    new = fuse_complete_rankings(source, DIRECTIONS)
    assert old.n_engines_success.tolist() == [2, 0]
    assert old.consensus_score.notna().all()
    assert new.consensus_score.isna().all()
    assert new.consensus_policy.eq("rrf-finite-complete-case-v2").all()


@pytest.mark.parametrize("other", STRAIN_COLUMNS[1:])
def test_other_strain_fields_never_substitute_for_local_energy(other):
    source = covdock_row().drop(columns=[STRAIN_COLUMNS[0]])
    source[other] = 21.
    new = strict(source).iloc[0]
    assert pd.isna(new.local_strain_energy)
    assert new.strain_measurement_status == "MISSING"
    assert new.physical_validity == "UNKNOWN"
    assert not new.review_eligible
    assert new[other] == 21.
    assert assess_covdock_candidates(source).iloc[0].physical_validity == "FAIL"


def test_exact_local_energy_takes_precedence_without_merging_other_fields():
    result = strict(covdock_row(r_strain_Strain_Energy=100.)).iloc[0]
    assert result.local_strain_energy == 4.
    assert result.strain_source_field == STRAIN_COLUMNS[0]
    assert result.physical_validity == "PASS"
    assert result.review_eligible


@pytest.mark.parametrize('strain,rmsd,physical,consensus', [
    (4., 4.5, 'PASS', 'LOW'), (12., 1.5, 'WARN', 'HIGH'), (12., 4.5, 'WARN', 'LOW'),
])
def test_selection_eligibility_does_not_require_gate_a(strain, rmsd, physical, consensus):
    result = strict(covdock_row(r_strain_Local_Strain_Energy=strain,
                               af3_covdock_ligand_rmsd_angstrom=rmsd)).iloc[0]
    assert result.physical_validity == physical
    assert result.cross_method_consensus == consensus
    assert result.covdock_gate_detail == 'B1'
    assert result.review_eligible


@pytest.mark.parametrize("bad", [np.inf, -np.inf, "invalid", None])
def test_invalid_local_energy_cannot_fallback_to_valid_global_energy(bad):
    result = strict(covdock_row(r_strain_Local_Strain_Energy=bad, r_strain_Strain_Energy=4.)).iloc[0]
    assert result.physical_validity == "UNKNOWN"
    assert not result.review_eligible


@pytest.mark.parametrize("changes", [{"engine_status": "FAILED"}, {"raw_score": np.inf}, {"raw_score": None}])
def test_engine_failure_is_separate_from_complete_physical_evidence(changes):
    result = strict(covdock_row(**changes)).iloc[0]
    assert result.physical_validity == "PASS"
    assert result.execution_status != "SUCCESS"
    assert result.covdock_gate == "C"
    assert not result.review_eligible


def test_missing_evidence_and_actual_failure_are_distinct():
    missing = strict(covdock_row(engine_status="FAILED", severe_clash=None)).iloc[0]
    failed = strict(covdock_row(engine_status="FAILED", severe_clash=True)).iloc[0]
    assert missing.physical_validity == "UNKNOWN"
    assert failed.physical_validity == "FAIL"


@pytest.mark.parametrize("energy,physical", [(10., "PASS"), (10.01, "WARN"), (20., "WARN"), (20.01, "FAIL")])
def test_numeric_thresholds_have_not_been_recalibrated(energy, physical):
    result = strict(covdock_row(r_strain_Local_Strain_Energy=energy)).iloc[0]
    assert result.physical_validity == physical
    assert not result.thresholds_calibrated


def test_unknown_is_not_selected_by_new_writer_and_legacy_replays(tmp_path):
    source = pd.concat([
        covdock_row(),
        covdock_row(input_sdf_index=1, compound_id="missing", severe_clash=None),
        covdock_row(input_sdf_index=2, compound_id="no-consensus", af3_covdock_ligand_rmsd_angstrom=None),
        covdock_row(input_sdf_index=3, compound_id="engine-failed", engine_status="FAILED"),
    ], ignore_index=True)
    original = source.copy(deep=True)
    old = gate_covdock_candidates(source, tmp_path / "legacy", min_success_rate=.5)
    new = gate_covdock_candidates(source, tmp_path / "strict", min_success_rate=.5, algorithm_version=STRICT_ALGORITHM_VERSION)
    assert old["algorithm_version"] == ALGORITHM_VERSION
    assert new["algorithm_version"] == STRICT_ALGORITHM_VERSION
    assert new["quality"]["input_identity_count"] == len(source)
    assert new["policy"]["thresholds_calibrated"] is False
    assert pd.read_parquet(tmp_path / "strict/final_candidates.parquet").compound_id.tolist() == ["synthetic"]
    written = pd.read_parquet(tmp_path / "strict/stage5_gated_candidates.parquet").sort_values("input_sdf_index").reset_index(drop=True)
    pd.testing.assert_frame_equal(written, strict(source))
    pd.testing.assert_frame_equal(source, original)


def test_unknown_algorithm_fails_before_output(tmp_path):
    with pytest.raises(ValueError, match="algorithm version"):
        gate_covdock_candidates(covdock_row(), tmp_path / "absent", algorithm_version="typo")
    assert not (tmp_path / "absent").exists()
