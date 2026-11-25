from __future__ import annotations

from pathlib import Path

import pandas as pd
from rdkit import Chem

from covscreen.af3_selection import select_af3_candidates


def test_af3_selection_ranks_mpae_and_preserves_failures(tmp_path: Path):
    scores = pd.DataFrame(
        {
            "input_sdf_index": [10, 20, 30, 40],
            "compound_id": ["a", "b", "c", "d"],
            "standardized_smiles": [
                "C=CC(=O)NCC",
                "C=CC(=O)NCCC",
                "C=CC(=O)NCCCC",
                "C=CC(=O)NCCCCC",
            ],
            "engine_status": ["SUCCESS", "SUCCESS", "FAILED", "SUCCESS"],
            "failure_reason": [None, None, "missing_output", None],
            "raw_score": [2.5, 1.0, float("nan"), 3.0],
        }
    )
    manifest = select_af3_candidates(
        scores,
        tmp_path,
        top_n=2,
        min_coverage=0.75,
    )
    selected = pd.read_parquet(tmp_path / "stage4_top_candidates.parquet")
    assert selected["compound_id"].tolist() == ["b", "a"]
    assert selected["af3_mpae_rank"].tolist() == [1, 2]
    failures = pd.read_parquet(tmp_path / "af3_failures.parquet")
    assert failures["compound_id"].tolist() == ["c"]
    supplier = Chem.SDMolSupplier(str(tmp_path / "stage4_top_candidates.sdf"))
    assert sum(molecule is not None for molecule in supplier) == 2
    assert manifest["status"] == "completed"
    assert manifest["quality"]["coverage"] == 0.75
    assert all(manifest["quality_gates"].values())


def test_af3_selection_rejects_nonfinite_and_negative_mpae(tmp_path: Path):
    scores = pd.DataFrame({
        "input_sdf_index": range(5),
        "compound_id": ["valid", "inf", "neginf", "negative", "nan"],
        "standardized_smiles": ["C=CC(=O)NCC"] * 5,
        "engine_status": ["SUCCESS"] * 5,
        "raw_score": [1.2, float("inf"), float("-inf"), -1, float("nan")],
    })
    manifest = select_af3_candidates(scores, tmp_path, top_n=5, min_coverage=0.95)
    selected = pd.read_parquet(tmp_path / "stage4_top_candidates.parquet")
    failures = pd.read_parquet(tmp_path / "af3_failures.parquet")
    assert selected["compound_id"].tolist() == ["valid"]
    assert len(failures) == 4
    assert failures["engine_status"].eq("FAILED").all()
    assert failures["failure_reason"].eq("invalid_af3_mpae").all()
    assert manifest["quality"]["coverage"] == 0.2
    assert manifest["status"] != "completed"
