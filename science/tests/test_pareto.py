from pathlib import Path

import pandas as pd
import pytest

from covscreen.pareto import select_pareto_candidates


def test_pareto_uses_non_dominated_fronts_and_scaffold_round_robin(tmp_path: Path):
    candidates = pd.DataFrame(
        {
            "input_sdf_index": [0, 1, 2, 3, 4],
            "compound_id": ["a", "b", "c", "d", "e"],
            "standardized_smiles": [
                "c1ccccc1C=CC(=O)N",
                "c1ccccc1CC=CC(=O)N",
                "C1CCCCC1C=CC(=O)N",
                "C=CC(=O)NCC",
                "C=CC(=O)NCCC",
            ],
            "scaffold_smiles": ["c1ccccc1", "c1ccccc1", "C1CCCCC1", "", ""],
            "covdock_gate": ["A", "A", "B", "C", "A"],
            "covdock_gate_detail": ["A", "A", "B1", "C", "A"],
            "physical_validity": ["PASS", "PASS", "WARN", "FAIL", "PASS"],
            "cross_method_consensus": ["HIGH", "HIGH", "LOW", "HIGH", "HIGH"],
            "af3_mpae": [1.0, 2.0, 0.5, 0.1, 3.0],
            "af3_covdock_ligand_rmsd_angstrom": [1.5, 2.5, 4.0, 1.0, 2.0],
            "local_strain_energy": [4.0, 5.0, 3.0, 2.0, 6.0],
            "af3_mpae_rank": [2, 3, 1, 0, 4],
            "property_fit_score": [0.5, 0.7, 0.9, 0.9, 0.4],
            "high_reactivity_alert": [False, False, False, False, True],
            "aggregation_risk": [False, False, False, False, True],
            "pains_alert": ["", "", "", "", "alert"],
        }
    )
    manifest = select_pareto_candidates(candidates, tmp_path, final_n=2)
    assert manifest["status"] == "completed"
    assert manifest["selection_policy"]["weighted_sum_used"] is False
    assert manifest["selection_policy"]["gate_labels_are_objectives"] is False
    assert "gate_rank" not in manifest["selection_policy"]["objectives"]
    assert manifest["evidence"]["decision_evidence_status"] == (
        "baseline_ready_optional_evidence_missing"
    )
    all_candidates = pd.read_parquet(tmp_path / "stage6_pareto_all_candidates.parquet")
    assert not all_candidates.set_index("compound_id").loc["d", "pareto_eligible"]
    assert all_candidates.set_index("compound_id").loc["e", "pareto_front"] > 1
    selected = pd.read_parquet(tmp_path / "stage6_final_candidates.parquet")
    assert selected["compound_id"].tolist() == ["a", "c"]
    assert selected["scaffold_smiles"].nunique() == 2
    assert (tmp_path / "stage6_candidate_cards.json").is_file()


def test_pareto_fails_gate_when_required_risk_evidence_is_missing(tmp_path: Path):
    candidates = pd.DataFrame(
        {
            "input_sdf_index": [0],
            "compound_id": ["a"],
            "standardized_smiles": ["C=CC(=O)NCC"],
            "covdock_gate": ["A"],
            "af3_mpae": [1.0],
            "af3_covdock_ligand_rmsd_angstrom": [1.5],
            "local_strain_energy": [4.0],
            "property_fit_score": [0.5],
        }
    )
    manifest = select_pareto_candidates(candidates, tmp_path, final_n=1)
    assert manifest["status"] == "quality_gate_failed"
    assert manifest["quality_gates"]["baseline_objectives_available"] is False
    assert manifest["quality"]["selected_count"] == 0


def _ligand_candidates():
    return pd.DataFrame([{
        "input_sdf_index": i, "compound_id": f"candidate_{i}",
        "standardized_smiles": "C=CC(=O)NCC", "covdock_gate": "A",
        "physical_validity": "PASS", "independent_gate": gate,
        "independent_parent_mcs_fraction": 1.0, "af3_mpae": 1.0,
        "local_strain_energy": 4.0, "property_fit_score": 0.5,
        "high_reactivity_alert": False, "aggregation_risk": False,
        "pains_alert": "",
    } for i, gate in enumerate(["PASS", "FAIL", "UNKNOWN"])])


def test_ligand_pareto_excludes_independent_failure_even_with_physical_pass(tmp_path):
    manifest = select_pareto_candidates(_ligand_candidates(), tmp_path, profile="ligand-first")
    all_candidates = pd.read_parquet(tmp_path / "stage6_pareto_all_candidates.parquet")
    selected = pd.read_parquet(tmp_path / "stage6_final_candidates.parquet")
    assert selected.compound_id.tolist() == ["candidate_0"]
    assert all_candidates.ranking_policy.eq("ligand-first-pareto-v1.0").all()
    assert selected.ranking_policy.eq("ligand-first-pareto-v1.0").all()
    assert manifest["algorithm_version"] == "ligand-first-pareto-v1.0"
    assert manifest["quality"]["input_count"] == 3


def test_ligand_pareto_requires_independent_evaluation(tmp_path):
    with pytest.raises(ValueError, match="requires independent_gate"):
        select_pareto_candidates(_ligand_candidates().drop(columns="independent_gate"),
                                 tmp_path, profile="ligand-first")


def test_infinite_objective_is_not_a_valid_final_candidate(tmp_path):
    candidates = _ligand_candidates()
    candidates.loc[0, "af3_mpae"] = float("-inf")
    manifest = select_pareto_candidates(candidates, tmp_path, profile="ligand-first")
    assert manifest["quality"]["selected_count"] == 0
    assert manifest["status"] == "quality_gate_failed"
