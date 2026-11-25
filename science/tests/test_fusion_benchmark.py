import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import merge_pair, select_policy, simulate_fusions


def _tables():
    library = pd.DataFrame(
        {
            "compound_id": ["a", "b", "c", "d", "e"],
            "protomer_id": [0, 0, 0, 0, 0],
            "input_sdf_index": [0, 1, 2, 3, 4],
            "label": ["active", "decoy", "active", "decoy", "decoy"],
        }
    )
    af3 = library.iloc[:, :3].copy()
    af3["raw_score"] = [1.0, 2.0, 3.0, 4.0, np.nan]
    af3["engine_status"] = ["SUCCESS"] * 4 + ["FAILED"]
    covdock = library.iloc[:, :3].copy()
    covdock["raw_score"] = [2.0, 1.0, 3.0, np.nan, 4.0]
    covdock["engine_status"] = ["SUCCESS"] * 3 + ["FAILED", "SUCCESS"]
    return library, af3, covdock


def test_pair_merge_preserves_every_input_and_failures():
    merged = merge_pair(*_tables())
    assert len(merged) == 5
    assert merged["af3_success"].sum() == 4
    assert merged["covdock_lo_success"].sum() == 4
    assert merged["af3_score"].isna().sum() == 1


def test_simulated_cascade_is_budgeted_and_complete():
    fused, metrics = simulate_fusions(merge_pair(*_tables()), fractions=(0.4,))
    assert fused["cascade_40pct_selected"].sum() == 2
    assert set(fused["cascade_rrf_40pct_rank"]) == {1, 2, 3, 4, 5}
    row = metrics.loc[metrics["method"].eq("cascade_rrf_40pct")].iloc[0]
    assert row["covdock_selected_n"] == 2


def test_policy_prefers_cheapest_eligible_cascade():
    _, metrics = simulate_fusions(merge_pair(*_tables()), fractions=(0.4, 0.8))
    policy = select_policy(
        metrics,
        bedroc_margin=1.0,
        top10_margin=5,
        top25_margin=5,
    )
    assert policy["status"] == "SELECTED"
    assert policy["covdock_selected_n"] == 2
