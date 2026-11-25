"""Evaluate an exploratory AF3 -> CovDock-LO -> RTMScore cascade."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import deterministic_order
from covscreen.metrics import summarize


METRICS = (
    "roc_auc",
    "adjusted_logauc_percent",
    "ef1_percent",
    "ef5_percent",
    "bedroc_alpha20",
    "top10_active",
    "top25_active",
    "top50_active",
)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _metric_row(frame: pd.DataFrame, method: str, rank_column: str) -> dict[str, object]:
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    return {"method": method, "rank_column": rank_column, **summarize(labels, frame[rank_column], "lower_is_better")}


def _markdown_table(frame: pd.DataFrame) -> str:
    columns = ["method", *METRICS]
    display = frame[columns]
    rows = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for values in display.itertuples(index=False, name=None):
        rows.append(
            "| "
            + " | ".join(
                f"{value:.4f}" if isinstance(value, float) else str(value)
                for value in values
            )
            + " |"
        )
    return "\n".join(rows)


def _rank_three_stage(frame: pd.DataFrame, top_n: int, rrf_k: float) -> pd.DataFrame:
    out = frame.copy()
    selected_mask = out["af3_rank"].le(top_n)
    selected = out.loc[selected_mask].copy()
    if len(selected) != top_n or selected["rtmscore"].isna().any():
        raise ValueError("Every AF3-selected candidate must have one finite RTMScore")
    selected["af3_subset_rank"] = deterministic_order(
        selected, "af3_score", "lower_is_better"
    )
    selected["covdock_subset_rank"] = deterministic_order(
        selected, "covdock_lo_score", "lower_is_better"
    )
    selected["rtmscore_subset_rank"] = deterministic_order(
        selected, "rtmscore", "higher_is_better"
    )
    selected["three_stage_rrf_score"] = (
        1.0 / (rrf_k + selected["af3_subset_rank"])
        + 1.0 / (rrf_k + selected["covdock_subset_rank"])
        + 1.0 / (rrf_k + selected["rtmscore_subset_rank"])
    )
    selected_order = selected.sort_values(
        ["three_stage_rrf_score", "af3_subset_rank", "input_sdf_index"],
        ascending=[False, True, True],
        kind="stable",
    )
    remainder = out.loc[~selected_mask].sort_values("af3_rank", kind="stable")
    order = list(selected_order.index) + list(remainder.index)
    out["three_stage_rrf_rank"] = 0
    out.loc[order, "three_stage_rrf_rank"] = np.arange(1, len(out) + 1)
    for column in (
        "af3_subset_rank",
        "covdock_subset_rank",
        "rtmscore_subset_rank",
        "three_stage_rrf_score",
    ):
        out[column] = selected[column]
    return out


def _bootstrap_fixed_rank_deltas(
    frame: pd.DataFrame,
    *,
    iterations: int,
    seed: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, dict[str, float]]]]:
    rng = np.random.default_rng(seed)
    active = frame.loc[frame["label"].eq("active")]
    decoy = frame.loc[frame["label"].eq("decoy")]
    comparisons = {
        "three_stage_vs_af3": ("three_stage_rrf_rank", "af3_rank"),
        "three_stage_vs_two_stage": (
            "three_stage_rrf_rank",
            "cascade_rrf_05pct_rank",
        ),
    }
    records: list[dict[str, object]] = []
    for iteration in range(iterations):
        sampled = pd.concat(
            [
                active.iloc[rng.integers(0, len(active), len(active))],
                decoy.iloc[rng.integers(0, len(decoy), len(decoy))],
            ],
            ignore_index=True,
        )
        labels = sampled["label"].map({"active": 1, "decoy": 0}).to_numpy()
        row: dict[str, object] = {"bootstrap_iteration": iteration}
        for comparison, (new_rank, baseline_rank) in comparisons.items():
            new_metrics = summarize(labels, sampled[new_rank], "lower_is_better")
            baseline_metrics = summarize(labels, sampled[baseline_rank], "lower_is_better")
            for metric in METRICS:
                row[f"{comparison}__{metric}"] = float(new_metrics[metric]) - float(
                    baseline_metrics[metric]
                )
        records.append(row)
    deltas = pd.DataFrame(records)
    summary: dict[str, dict[str, dict[str, float]]] = {}
    for comparison in comparisons:
        summary[comparison] = {}
        for metric in METRICS:
            values = deltas[f"{comparison}__{metric}"].to_numpy(dtype=float)
            nonpositive = (np.count_nonzero(values <= 0) + 1) / (len(values) + 1)
            nonnegative = (np.count_nonzero(values >= 0) + 1) / (len(values) + 1)
            summary[comparison][metric] = {
                "mean_delta": float(np.mean(values)),
                "ci95_low": float(np.quantile(values, 0.025)),
                "ci95_high": float(np.quantile(values, 0.975)),
                "two_sided_bootstrap_p": float(
                    min(1.0, 2.0 * min(nonpositive, nonnegative))
                ),
                "probability_delta_gt_zero": float(np.mean(values > 0)),
            }
    return deltas, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fused-table", type=Path, required=True)
    ap.add_argument("--rtmscore", type=Path, required=True)
    ap.add_argument("--rtmscore-manifest", type=Path, required=True)
    ap.add_argument("--two-stage-summary", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--top-n", type=int, default=21)
    ap.add_argument("--rrf-k", type=float, default=60.0)
    ap.add_argument("--bootstrap-iterations", type=int, default=5000)
    ap.add_argument("--bootstrap-seed", type=int, default=20260826)
    args = ap.parse_args()

    fused = _read(args.fused_table)
    rtmscore = _read(args.rtmscore)[
        ["input_sdf_index", "raw_score", "engine_status"]
    ].rename(
        columns={"raw_score": "rtmscore", "engine_status": "rtmscore_status"}
    )
    if rtmscore["input_sdf_index"].duplicated().any():
        raise ValueError("RTMScore table contains duplicate input identities")
    merged = fused.merge(rtmscore, on="input_sdf_index", how="left", validate="one_to_one")
    ranked = _rank_three_stage(merged, args.top_n, args.rrf_k)

    metric_rows = [
        _metric_row(ranked, "af3_mpae", "af3_rank"),
        _metric_row(ranked, "frozen_two_stage_rrf", "cascade_rrf_05pct_rank"),
        _metric_row(ranked, "exploratory_three_stage_rrf", "three_stage_rrf_rank"),
    ]
    metrics = pd.DataFrame(metric_rows)
    deltas, bootstrap = _bootstrap_fixed_rank_deltas(
        ranked,
        iterations=args.bootstrap_iterations,
        seed=args.bootstrap_seed,
    )
    observed: dict[str, dict[str, float]] = {}
    three = metrics.loc[metrics["method"].eq("exploratory_three_stage_rrf")].iloc[0]
    for comparison, method in (
        ("three_stage_vs_af3", "af3_mpae"),
        ("three_stage_vs_two_stage", "frozen_two_stage_rrf"),
    ):
        baseline = metrics.loc[metrics["method"].eq(method)].iloc[0]
        observed[comparison] = {
            metric: float(three[metric]) - float(baseline[metric]) for metric in METRICS
        }

    rtms_manifest = json.loads(args.rtmscore_manifest.read_text())
    two_stage = json.loads(args.two_stage_summary.read_text())
    projected_runtime_hours = float(two_stage["projected_runtime_hours"]) + float(
        rtms_manifest["runtime_seconds"]
    ) / 3600.0
    summary = {
        "status": "PASS_EXPLORATORY",
        "method": "AF3 top-21 -> CovDock-LO affinity -> RTMScore -> equal-weight three-rank RRF",
        "top_n": args.top_n,
        "rrf_k": args.rrf_k,
        "rtmscore_success": int(ranked["rtmscore_status"].eq("SUCCESS").sum()),
        "observed_deltas": observed,
        "bootstrap_iterations": args.bootstrap_iterations,
        "bootstrap_seed": args.bootstrap_seed,
        "bootstrap_fixed_rank_deltas": bootstrap,
        "projected_runtime_hours": projected_runtime_hours,
        "confirmatory_status": "NOT_CONFIRMATORY_LABELS_ALREADY_INSPECTED",
        "required_next_validation": (
            "Freeze the three-stage rule without further tuning and evaluate it on at least "
            "one new held-out covalent target; multiple targets are required for a repeatable-gain claim."
        ),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(args.output_dir / "egfr_mini_three_stage_ranked.csv", index=False)
    ranked.to_parquet(args.output_dir / "egfr_mini_three_stage_ranked.parquet", index=False)
    metrics.to_csv(args.output_dir / "three_stage_metrics.csv", index=False)
    deltas.to_csv(args.output_dir / "three_stage_bootstrap_deltas.csv", index=False)
    (args.output_dir / "three_stage_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# EGFR-mini 三阶段级联探索性评价",
        "",
        "流程：AF3–mPAE Top-21 → CovDock-LO pose/affinity → RTMScore → 等权三排序 RRF（k=60）。",
        "",
        _markdown_table(metrics),
        "",
        "## 结论边界",
        "",
        "- 三阶段结果为探索性分析；EGFR 标签已经查看，不能称为新的盲法外部验证。",
        "- bootstrap 使用固定预测排序，给出候选层面的配对不确定性，不证明跨靶点可重复性。",
        "- 下一步应冻结该三阶段规则，在新的未查看靶点上原样验证。",
        "",
    ]
    (args.output_dir / "EGFR_MINI_THREE_STAGE_EXPLORATORY_REPORT.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
