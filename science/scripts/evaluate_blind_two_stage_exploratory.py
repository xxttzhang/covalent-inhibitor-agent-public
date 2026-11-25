"""Post-unblinding ablation of AF3 -> CovDock-LO two-stage ranking.

This analysis is exploratory because it was defined after the five-target
answer key was revealed.  It does not alter any locked prediction file.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import deterministic_order
from covscreen.metrics import summarize


METRICS = (
    "adjusted_logauc_percent",
    "bedroc_alpha20",
    "roc_auc",
    "ef1_percent",
    "ef5_percent",
    "top10_active",
    "top25_active",
    "top50_active",
)


def final_rank(frame: pd.DataFrame, selected_order: pd.Index) -> pd.Series:
    selected = set(selected_order)
    remainder = frame.loc[~frame.index.isin(selected)].sort_values(
        "af3_rank", kind="stable"
    )
    order = list(selected_order) + list(remainder.index)
    rank = pd.Series(index=frame.index, dtype=int)
    rank.loc[order] = np.arange(1, len(frame) + 1, dtype=int)
    return rank.astype(int)


def metric_row(frame: pd.DataFrame, method: str, rank_column: str) -> dict[str, object]:
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    return {
        "target_id": str(frame["panel_target_id"].iloc[0]),
        "method": method,
        **summarize(labels, frame[rank_column].to_numpy(), "lower_is_better"),
    }


def exact_one_sided_sign_p(values: np.ndarray) -> float:
    values = values[np.isfinite(values)]
    nonzero = values[values != 0]
    positives = int(np.count_nonzero(nonzero > 0))
    n = len(nonzero)
    if n == 0:
        return 1.0
    return float(sum(math.comb(n, k) for k in range(positives, n + 1)) / (2**n))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ranked-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=21)
    parser.add_argument("--rrf-k", type=float, default=60.0)
    args = parser.parse_args()

    source = pd.read_parquet(args.ranked_input)
    metric_rows: list[dict[str, object]] = []
    ranked_frames: list[pd.DataFrame] = []
    for target_id, target in source.groupby("panel_target_id", sort=False):
        frame = target.copy()
        frame["af3_rank"] = deterministic_order(frame, "af3_score", "lower_is_better")
        selected = frame.loc[frame["af3_rank"].le(args.top_n)].copy()
        if len(selected) != args.top_n:
            raise ValueError(f"{target_id}: expected {args.top_n} selected rows")

        selected["covdock_subset_rank"] = deterministic_order(
            selected, "covdock_score", "lower_is_better"
        )
        covdock_order = selected.sort_values(
            ["covdock_subset_rank", "af3_rank", "input_sdf_index"], kind="stable"
        ).index
        frame["two_stage_covdock_rank"] = final_rank(frame, covdock_order)

        selected["af3_subset_rank"] = deterministic_order(
            selected, "af3_score", "lower_is_better"
        )
        selected["two_stage_rrf_score"] = (
            1.0 / (args.rrf_k + selected["af3_subset_rank"])
            + 1.0 / (args.rrf_k + selected["covdock_subset_rank"])
        )
        rrf_order = selected.sort_values(
            ["two_stage_rrf_score", "af3_subset_rank", "input_sdf_index"],
            ascending=[False, True, True],
            kind="stable",
        ).index
        frame["two_stage_rrf_rank"] = final_rank(frame, rrf_order)

        metric_rows.extend(
            [
                metric_row(frame, "af3_mpae", "af3_rank"),
                metric_row(frame, "af3_top5_covdock_rerank", "two_stage_covdock_rank"),
                metric_row(frame, "af3_covdock_equal_rrf", "two_stage_rrf_rank"),
            ]
        )
        ranked_frames.append(frame)

    metrics = pd.DataFrame(metric_rows)
    baseline = metrics.loc[metrics["method"].eq("af3_mpae")].set_index("target_id")
    delta_rows: list[dict[str, object]] = []
    cross_target: dict[str, object] = {}
    for method in ("af3_top5_covdock_rerank", "af3_covdock_equal_rrf"):
        current = metrics.loc[metrics["method"].eq(method)].set_index("target_id")
        for target_id in baseline.index:
            delta_rows.append(
                {
                    "target_id": target_id,
                    "method": method,
                    **{
                        metric: float(current.loc[target_id, metric])
                        - float(baseline.loc[target_id, metric])
                        for metric in METRICS
                    },
                }
            )
        method_delta = pd.DataFrame(
            [row for row in delta_rows if row["method"] == method]
        )
        cross_target[method] = {
            metric: {
                "mean_delta": float(method_delta[metric].mean()),
                "positive_targets": int((method_delta[metric] > 0).sum()),
                "one_sided_exact_sign_p": exact_one_sided_sign_p(
                    method_delta[metric].to_numpy(dtype=float)
                ),
            }
            for metric in METRICS
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(args.output_dir / "per_target_metrics.csv", index=False)
    pd.DataFrame(delta_rows).to_csv(args.output_dir / "per_target_deltas.csv", index=False)
    pd.concat(ranked_frames, ignore_index=True).to_parquet(
        args.output_dir / "all_targets_ranked.parquet", index=False
    )
    summary = {
        "status": "EXPLORATORY_POST_UNBLINDING",
        "top_n": args.top_n,
        "rrf_k": args.rrf_k,
        "cross_target": cross_target,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
