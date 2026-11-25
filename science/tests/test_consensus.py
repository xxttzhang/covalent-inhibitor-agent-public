import numpy as np
import pandas as pd

from covscreen.consensus import fuse_rankings, rank_scores
from covscreen.consensus_analysis import analyze_consensus


def test_rank_scores_places_failure_last():
    ranks, percentiles = rank_scores(
        pd.Series([0.5, np.nan, 0.8]), "higher_is_better"
    )
    assert ranks.tolist() == [2, 3, 1]
    assert percentiles.iloc[2] > percentiles.iloc[0] > percentiles.iloc[1]


def test_rank_fusion_tracks_missing_engine_and_conflicts():
    frame = pd.DataFrame(
        {
            "compound_id": ["a", "b", "c"],
            "carsidock_score": [3.0, 2.0, 1.0],
            "af3_score": [1.0, np.nan, 3.0],
        }
    )
    fused = fuse_rankings(
        frame,
        {
            "carsidock_score": "higher_is_better",
            "af3_score": "higher_is_better",
        },
    )
    assert fused.loc[1, "conflict_type"] == "MISSING_ENGINE"
    assert set(fused["consensus_rank"]) == {1, 2, 3}


def test_consensus_analysis_reports_overlap_and_missingness():
    frame = pd.DataFrame(
        {
            "compound_id": ["a", "b", "c"],
            "protomer_id": ["p", "p", "p"],
            "input_sdf_index": [0, 1, 2],
            "carsidock_score": [1.0, 2.0, None],
            "af3_score": [1.1, 2.1, 3.1],
        }
    )
    fused = fuse_rankings(
        frame,
        {"carsidock_score": "lower_is_better", "af3_score": "lower_is_better"},
    )
    report = analyze_consensus(fused, ["carsidock_score", "af3_score"], top_ns=(1,))
    assert report["missing_by_engine"]["carsidock_score"] == 1
    assert report["top_n_overlap"]["1"]["carsidock_score__af3_score"]["intersection_count"] == 1
    assert report["pairwise_spearman"]["carsidock_score__af3_score"]["n_common"] == 2
