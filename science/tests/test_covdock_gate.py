from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from covscreen.covdock_gate import gate_covdock_candidates, merge_pose_consistency


@pytest.mark.parametrize("route", ["ligand", "library"])
def test_optional_af3_keeps_physical_checks_and_legacy_replay(tmp_path, route):
    from covscreen.covdock_gate import assess_covdock_candidates, LIGAND_ALGORITHM_VERSION, LIBRARY_ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION
    version = LIGAND_ALGORITHM_VERSION if route == "ligand" else LIBRARY_ALGORITHM_VERSION
    scores = pd.DataFrame({
        'input_sdf_index': [0, 1, 2, 3, 4], 'compound_id': ['good', 'missing', 'clash', 'strain', 'failed'],
        'engine_status': ['SUCCESS'] * 4 + ['FAILED'], 'raw_score': [-8.0] * 5,
        'covalent_link_present': [True] * 5, 'covalent_bond_length_angstrom': [1.82] * 5,
        'severe_clash': [False, False, True, False, False],
        'r_strain_Local_Strain_Energy': [4.0, float('nan'), 4.0, 25.0, 4.0]})
    old = assess_covdock_candidates(scores, algorithm_version=STRICT_ALGORITHM_VERSION)
    assert old.loc[0, 'covdock_gate_detail'] == 'B2' and not old.loc[0, 'review_eligible']
    new = assess_covdock_candidates(scores, algorithm_version=version)
    assert new['review_eligible'].tolist() == [True, False, False, False, False]
    assert new.loc[0, 'cross_method_consensus'] == 'NA'
    assert new.loc[1, 'physical_validity'] == 'UNKNOWN'
    assert new.loc[2, 'physical_validity'] == new.loc[3, 'physical_validity'] == 'FAIL'
    manifest = gate_covdock_candidates(scores, tmp_path, algorithm_version=version)
    assert manifest['policy']['cross_method_consensus_required'] is False
    assert pd.read_parquet(tmp_path / 'final_candidates.parquet')['compound_id'].tolist() == ['good']


def test_merge_pose_consistency_is_identity_exact():
    scores = pd.DataFrame(
        {"input_sdf_index": [2, 1], "compound_id": ["b", "a"]}
    )
    consistency = pd.DataFrame(
        {
            "input_sdf_index": [1, 2],
            "af3_covdock_ligand_rmsd_angstrom": [1.2, 3.4],
            "pose_consistency_status": ["SUCCESS", "SUCCESS"],
        }
    )
    merged = merge_pose_consistency(scores, consistency)
    assert merged["compound_id"].tolist() == ["b", "a"]
    assert merged["af3_covdock_ligand_rmsd_angstrom"].tolist() == [3.4, 1.2]


def test_covdock_gate_preserves_af3_order_and_assigns_abc(tmp_path: Path):
    scores = pd.DataFrame(
        {
            "input_sdf_index": [1, 2, 3, 4, 5],
            "compound_id": ["a", "b", "c", "d", "e"],
            "af3_mpae_rank": [2, 1, 3, 4, 5],
            "engine_status": ["SUCCESS", "SUCCESS", "SUCCESS", "FAILED", "SUCCESS"],
            "raw_score": [-7.0, -8.0, -6.0, float("nan"), -5.5],
            "covalent_link_present": [True, True, False, None, True],
            "covalent_bond_length_angstrom": [1.82, 1.84, 1.80, float("nan"), 1.85],
            "severe_clash": [False, False, False, None, False],
            "r_strain_Local_Strain_Energy": [4.0, 5.0, 3.0, float("nan"), 6.0],
            "af3_covdock_ligand_rmsd_angstrom": [1.5, float("nan"), 1.0, float("nan"), 4.5],
        }
    )
    manifest = gate_covdock_candidates(
        scores,
        tmp_path,
        final_n=3,
        min_success_rate=0.75,
    )
    gated = pd.read_parquet(tmp_path / "stage5_gated_candidates.parquet")
    assert gated.set_index("compound_id").loc["a", "covdock_gate"] == "A"
    assert gated.set_index("compound_id").loc["b", "covdock_gate"] == "B"
    assert gated.set_index("compound_id").loc["b", "covdock_gate_detail"] == "B2"
    assert gated.set_index("compound_id").loc["c", "covdock_gate"] == "C"
    assert gated.set_index("compound_id").loc["d", "covdock_gate"] == "C"
    assert gated.set_index("compound_id").loc["e", "covdock_gate_detail"] == "B1"
    assert gated.set_index("compound_id").loc["e", "physical_validity"] == "PASS"
    assert gated.set_index("compound_id").loc["e", "cross_method_consensus"] == "LOW"
    final = pd.read_parquet(tmp_path / "final_candidates.parquet")
    assert final["compound_id"].tolist() == ["a", "e", "b"]
    assert manifest["quality"]["gate_counts"] == {"A": 1, "B": 2, "C": 2}
    assert manifest["quality"]["detailed_gate_counts"] == {
        "A": 1,
        "B1": 1,
        "B2": 1,
        "C": 2,
    }
    assert all(manifest["quality_gates"].values())
