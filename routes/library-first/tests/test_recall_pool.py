from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
from rdkit import Chem

from covscreen.recall_pool import build_recall_pool


def _load_rtmscore_script():
    path = Path(__file__).resolve().parents[3] / "science/scripts" / "run_rtmscore_existing_poses.py"
    spec = importlib.util.spec_from_file_location("run_rtmscore_existing_poses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rtmscore_identity_defaults_support_discovery_tables():
    module = _load_rtmscore_script()
    source = pd.DataFrame(
        {
            "input_sdf_index": [7],
            "compound_id": ["covindb-example"],
            "engine_status": ["SUCCESS"],
        }
    )
    result = module.normalize_identity_columns(source)
    assert result.loc[0, "label"] == "unknown"
    assert result.loc[0, "protomer_id"] == "0"
    assert result.loc[0, "parent_id"] == "covindb-example"
    assert result.loc[0, "matched_active_id"] == ""


def test_recall_pool_uses_score_diversity_and_failure_rescue(tmp_path: Path):
    smiles = [
        "CC(=O)NCC1=CC=CC=C1",
        "C=CC(=O)NCC1=CC=CC=C1",
        "C=CC(=O)NCC1=CC=CN=C1",
        "C=CC(=O)NCC1=CC=CC2=CC=CC=C12",
        "C=CC(=O)NCC1CCCCC1",
        "C=CC(=O)NCC1=CC=CO1",
        "C=CC(=O)NCC1=CC=CS1",
        "C=CC(=O)NCC1=CC=NN1",
        "C=CC(=O)NCCC1=CC=CC=C1",
        "C=CC(=O)NCCCC1=CC=CC=C1",
    ]
    carsi = pd.DataFrame(
        {
            "input_sdf_index": range(10),
            "compound_id": [f"compound-{index}" for index in range(10)],
            "standardized_smiles": smiles,
            "rank": range(1, 11),
            "channel_score": [1.0 - index / 20 for index in range(10)],
            "reference_similarity": [0.9 - index / 20 for index in range(10)],
            "engine_status": ["SUCCESS"] * 8 + ["FAILED", "FAILED"],
            "failure_reason": [None] * 8 + ["pose_failed", "pose_failed"],
            "raw_score": [-10.0 + index for index in range(8)] + [float("nan")] * 2,
        }
    )
    rtm = pd.DataFrame(
        {
            "input_sdf_index": range(10),
            "compound_id": [f"compound-{index}" for index in range(10)],
            "engine_status": ["SUCCESS"] * 8 + ["FAILED", "FAILED"],
            "failure_reason": [None] * 8 + ["upstream_failed", "upstream_failed"],
            "raw_score": [0.1 + index / 10 for index in range(8)] + [float("nan")] * 2,
        }
    )
    carsi_path = tmp_path / "carsidock.parquet"
    rtm_path = tmp_path / "rtmscore.parquet"
    carsi.to_parquet(carsi_path, index=False)
    rtm.to_parquet(rtm_path, index=False)

    manifest = build_recall_pool(
        carsi_path,
        tmp_path / "output",
        rtmscore_scores=rtm_path,
        top_n=6,
        score_union_n=3,
        diversity_n=2,
        rescue_n=1,
        min_carsidock_success_rate=0.75,
    )

    selected = pd.read_parquet(tmp_path / "output" / "stage3_recall_pool.parquet")
    assert len(selected) == 6
    assert not selected["input_sdf_index"].duplicated().any()
    assert set(selected["stage3_selection_channel"]) >= {
        "score_union",
        "scaffold_diversity",
        "failure_rescue",
    }
    rescued = selected.loc[selected["stage3_selection_channel"].eq("failure_rescue")]
    assert len(rescued) == 1
    assert not bool(rescued.iloc[0]["carsidock_success"])
    supplier = Chem.SDMolSupplier(
        str(tmp_path / "output" / "stage3_recall_pool.sdf"), removeHs=False
    )
    assert sum(molecule is not None for molecule in supplier) == 6
    assert manifest["status"] == "completed"
    assert manifest["quality"]["identity_loss_count"] == 0
    assert all(manifest["quality_gates"].values())
