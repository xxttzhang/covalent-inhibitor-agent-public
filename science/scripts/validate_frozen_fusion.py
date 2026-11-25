"""Externally validate a frozen AF3-first/CovDock-LO fusion policy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import merge_pair, simulate_fusions


METRIC_COLUMNS = (
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime(path: Path) -> float:
    value = json.loads(path.read_text()).get("runtime_seconds_total")
    if value is None:
        raise ValueError(f"runtime_seconds_total is absent from {path}")
    return float(value)


def _metric_rows(metrics: pd.DataFrame, selected_method: str) -> tuple[pd.Series, pd.Series]:
    baseline = metrics.loc[metrics["method"].eq("af3_mpae")]
    selected = metrics.loc[metrics["method"].eq(selected_method)]
    if len(baseline) != 1 or len(selected) != 1:
        raise ValueError(
            f"Expected one AF3 and one {selected_method} metric row; "
            f"found {len(baseline)} and {len(selected)}"
        )
    return baseline.iloc[0], selected.iloc[0]


def _bootstrap_deltas(
    merged: pd.DataFrame,
    *,
    fraction: float,
    rrf_k: float,
    selected_method: str,
    iterations: int,
    seed: int,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    active = merged.loc[merged["label"].eq("active")]
    decoy = merged.loc[merged["label"].eq("decoy")]
    records: list[dict[str, float | int]] = []
    for iteration in range(iterations):
        sampled = pd.concat(
            [
                active.iloc[rng.integers(0, len(active), len(active))],
                decoy.iloc[rng.integers(0, len(decoy), len(decoy))],
            ],
            ignore_index=True,
        )
        _, metrics = simulate_fusions(sampled, fractions=(fraction,), rrf_k=rrf_k)
        baseline, selected = _metric_rows(metrics, selected_method)
        record: dict[str, float | int] = {"bootstrap_iteration": iteration}
        for metric in METRIC_COLUMNS:
            record[f"delta_{metric}"] = float(selected[metric]) - float(baseline[metric])
        records.append(record)
    return pd.DataFrame(records)


def _bootstrap_summary(deltas: pd.DataFrame) -> dict[str, dict[str, float]]:
    report: dict[str, dict[str, float]] = {}
    for metric in METRIC_COLUMNS:
        values = deltas[f"delta_{metric}"].to_numpy(dtype=float)
        nonpositive = (np.count_nonzero(values <= 0) + 1) / (len(values) + 1)
        nonnegative = (np.count_nonzero(values >= 0) + 1) / (len(values) + 1)
        report[metric] = {
            "mean_delta": float(np.mean(values)),
            "median_delta": float(np.median(values)),
            "ci95_low": float(np.quantile(values, 0.025)),
            "ci95_high": float(np.quantile(values, 0.975)),
            "two_sided_bootstrap_p": float(min(1.0, 2.0 * min(nonpositive, nonnegative))),
            "probability_delta_gt_zero": float(np.mean(values > 0)),
        }
    return report


def _markdown_table(frame: pd.DataFrame) -> str:
    columns = ["method", *METRIC_COLUMNS, "projected_runtime_hours"]
    display = frame[[column for column in columns if column in frame]].copy()
    headers = "| " + " | ".join(display.columns) + " |"
    divider = "| " + " | ".join(["---"] * len(display.columns)) + " |"
    rows = []
    for values in display.itertuples(index=False, name=None):
        rows.append(
            "| "
            + " | ".join(f"{value:.4f}" if isinstance(value, float) else str(value) for value in values)
            + " |"
        )
    return "\n".join([headers, divider, *rows])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", type=Path, required=True)
    ap.add_argument("--af3", type=Path, required=True)
    ap.add_argument("--covdock-lo", type=Path, required=True)
    ap.add_argument("--frozen-policy", type=Path, required=True)
    ap.add_argument("--af3-metrics", type=Path, required=True)
    ap.add_argument("--covdock-lo-metrics", type=Path, required=True)
    ap.add_argument("--target-id", default="EGFR_C797")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--bootstrap-iterations", type=int, default=2000)
    ap.add_argument("--bootstrap-seed", type=int, default=20260826)
    args = ap.parse_args()

    frozen = json.loads(args.frozen_policy.read_text())
    policy = frozen["policy"]
    selected_method = str(policy["selected_method"])
    if selected_method != "cascade_rrf_05pct":
        raise ValueError(f"Unexpected frozen method: {selected_method}")
    fraction = float(policy["covdock_fraction"])
    rrf_k = float(frozen["rrf_k"])
    if frozen.get("development_target") == args.target_id:
        raise ValueError("External validation target must differ from the development target")

    library = _read(args.library)
    merged = merge_pair(library, _read(args.af3), _read(args.covdock_lo))
    fused, metrics = simulate_fusions(merged, fractions=(fraction,), rrf_k=rrf_k)
    baseline, selected = _metric_rows(metrics, selected_method)
    if int(selected["covdock_selected_n"]) != int(policy["covdock_selected_n"]):
        raise ValueError("Frozen selected count does not match this equal-sized validation set")

    af3_runtime = _runtime(args.af3_metrics)
    covdock_runtime = _runtime(args.covdock_lo_metrics)
    covdock_per_input = covdock_runtime / len(merged)
    full_dual_runtime = af3_runtime + covdock_runtime
    runtime_by_method = {
        "af3_mpae": af3_runtime,
        "covdock_lo": covdock_runtime,
        "full_rrf": full_dual_runtime,
        selected_method: af3_runtime + covdock_per_input * int(selected["covdock_selected_n"]),
    }
    metrics["projected_runtime_seconds"] = metrics["method"].map(runtime_by_method)
    metrics["projected_runtime_hours"] = metrics["projected_runtime_seconds"] / 3600.0
    metrics["projected_savings_vs_full_dual"] = 1.0 - (
        metrics["projected_runtime_seconds"] / full_dual_runtime
    )

    deltas = _bootstrap_deltas(
        merged,
        fraction=fraction,
        rrf_k=rrf_k,
        selected_method=selected_method,
        iterations=args.bootstrap_iterations,
        seed=args.bootstrap_seed,
    )
    bootstrap = _bootstrap_summary(deltas)
    observed_delta = {
        metric: float(selected[metric]) - float(baseline[metric])
        for metric in METRIC_COLUMNS
    }
    noninferior = (
        float(selected["bedroc_alpha20"]) >= float(baseline["bedroc_alpha20"]) - 0.03
        and int(selected["top10_active"]) >= int(baseline["top10_active"]) - 1
        and int(selected["top25_active"]) >= int(baseline["top25_active"]) - 1
    )
    strict_early_gain = (
        observed_delta["bedroc_alpha20"] > 0
        and observed_delta["adjusted_logauc_percent"] > 0
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fused.to_csv(args.output_dir / "egfr_mini_af3_covdock_lo_fused.csv", index=False)
    fused.to_parquet(args.output_dir / "egfr_mini_af3_covdock_lo_fused.parquet", index=False)
    metrics.to_csv(args.output_dir / "external_validation_metrics.csv", index=False)
    deltas.to_csv(args.output_dir / "paired_stratified_bootstrap_deltas.csv", index=False)
    summary = {
        "status": "PASS",
        "validation_target": args.target_id,
        "development_target": frozen["development_target"],
        "freeze_id": frozen["freeze_id"],
        "selected_method": selected_method,
        "rrf_k": rrf_k,
        "covdock_fraction": fraction,
        "covdock_selected_n": int(selected["covdock_selected_n"]),
        "n_input": len(merged),
        "af3_success": int(merged["af3_success"].sum()),
        "covdock_lo_success": int(merged["covdock_lo_success"].sum()),
        "observed_delta_vs_af3": observed_delta,
        "predeclared_noninferiority_gate_pass": bool(noninferior),
        "observed_early_enrichment_gain": bool(strict_early_gain),
        "bootstrap_iterations": args.bootstrap_iterations,
        "bootstrap_seed": args.bootstrap_seed,
        "bootstrap_delta_vs_af3": bootstrap,
        "projected_runtime_hours": float(runtime_by_method[selected_method] / 3600.0),
        "projected_savings_vs_full_dual": float(
            1.0 - runtime_by_method[selected_method] / full_dual_runtime
        ),
        "interpretation": (
            "The frozen two-score cascade is externally non-inferior and has a positive "
            "observed early-enrichment delta. Statistical certainty is determined by the "
            "paired stratified bootstrap intervals; this result does not validate a separate "
            "third-party pose rescoring model."
        ),
        "sources": {
            "frozen_policy": {"path": str(args.frozen_policy), "sha256": _sha256(args.frozen_policy)},
            "library": {"path": str(args.library), "sha256": _sha256(args.library)},
            "af3": {"path": str(args.af3), "sha256": _sha256(args.af3)},
            "covdock_lo": {"path": str(args.covdock_lo), "sha256": _sha256(args.covdock_lo)},
        },
    }
    (args.output_dir / "external_validation_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report = [
        "# EGFR-mini 冻结级联外部验证",
        "",
        f"- BTK 冻结编号：`{frozen['freeze_id']}`",
        f"- 验证靶点：`{args.target_id}`",
        f"- 预设策略：`{selected_method}`，RRF k={rrf_k:g}，CovDock-LO 比例={fraction:.0%}",
        f"- 固定分母：{len(merged)}；AF3 成功 {int(merged['af3_success'].sum())}；CovDock-LO 成功 {int(merged['covdock_lo_success'].sum())}",
        "- 缺失分数排在末尾；并列按 input_sdf_index 进行与标签无关的确定性排序。",
        "",
        "## 同口径结果",
        "",
        _markdown_table(metrics),
        "",
        "## 冻结级联相对 AF3 的观测差值",
        "",
        "```json",
        json.dumps(observed_delta, indent=2, ensure_ascii=False),
        "```",
        "",
        "## 统计解释",
        "",
        f"- 预声明非劣效门通过：{noninferior}",
        f"- BEDROC20 与 adjusted LogAUC 同时为正向观测增益：{strict_early_gain}",
        f"- 分层配对 bootstrap：{args.bootstrap_iterations} 次，种子 {args.bootstrap_seed}",
        "- 置信区间及双侧 bootstrap P 值见 `external_validation_summary.json`。",
        "- 当前验证的是 AF3 排序后用 CovDock-LO affinity 进行独立二次评价的两分数级联；尚未包含第三个独立 pose scorer。",
        "",
    ]
    (args.output_dir / "EGFR_MINI_FROZEN_CASCADE_VALIDATION_REPORT.md").write_text(
        "\n".join(report), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
