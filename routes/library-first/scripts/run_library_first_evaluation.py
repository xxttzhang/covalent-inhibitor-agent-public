from __future__ import annotations

import _bootstrap  # resolve this release, independent of cwd

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = _bootstrap.PROJECT
sys.path.insert(0, str(ROOT / "src"))

from covscreen.metrics import LOGAUC_FORMULA_VERSION, summarize


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError("score table must be CSV or Parquet")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate an identity-mapped Library-first score table conservatively."
    )
    parser.add_argument("--score-table", type=Path, required=True)
    parser.add_argument("--engine-name", required=True)
    parser.add_argument(
        "--score-direction",
        choices=["higher_is_better", "lower_is_better"],
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument("--expected-active", type=int)
    parser.add_argument("--expected-decoy", type=int)
    args = parser.parse_args()

    frame = read_table(args.score_table)
    required = {"compound_id", "input_sdf_index", "label", "raw_score"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"score table is missing columns: {missing}")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError("score table has duplicate input_sdf_index values")
    labels = frame["label"].astype(str).map({"active": 1, "decoy": 0})
    if labels.isna().any():
        raise ValueError("label must contain only active or decoy")
    labels = labels.astype("int64")
    observed_active = int(labels.sum())
    observed_decoy = int(len(labels) - observed_active)
    expectations = {
        "rows": (len(frame), args.expected_rows),
        "active": (observed_active, args.expected_active),
        "decoy": (observed_decoy, args.expected_decoy),
    }
    mismatches = {
        name: {"observed": observed, "expected": expected}
        for name, (observed, expected) in expectations.items()
        if expected is not None and observed != expected
    }
    if mismatches:
        raise ValueError(f"benchmark denominator mismatch: {mismatches}")

    raw = pd.to_numeric(frame["raw_score"], errors="coerce")
    if "engine_status" in frame.columns:
        success = frame["engine_status"].astype(str).eq("SUCCESS") & raw.notna()
    else:
        success = raw.notna()
    if not success.any():
        raise ValueError("score table has no successful finite scores")
    finite = raw[success]
    worst = (
        float(finite.min()) - 1.0
        if args.score_direction == "higher_is_better"
        else float(finite.max()) + 1.0
    )
    evaluated = frame.copy()
    evaluated["conservative_score"] = raw.where(success, worst)
    evaluated["evaluation_success"] = success
    evaluated["conservative_rank"] = evaluated["conservative_score"].rank(
        method="first", ascending=args.score_direction == "lower_is_better"
    ).astype(int)
    evaluated = evaluated.sort_values("conservative_rank")

    report = {
        "engine": args.engine_name,
        "score_direction": args.score_direction,
        "formula_version": LOGAUC_FORMULA_VERSION,
        "n_input": int(len(frame)),
        "n_active": observed_active,
        "n_decoy": observed_decoy,
        "n_scoring_success": int(success.sum()),
        "n_failed": int((~success).sum()),
        "success_rate": float(success.mean()),
        "failure_policy": "rank_explicit_failures_after_successful_scored_compounds",
        "conservative": summarize(
            labels.astype(int).to_numpy(),
            evaluated.sort_index()["conservative_score"].to_numpy(),
            args.score_direction,
        ),
    }
    if success.sum() >= 2 and labels[success].nunique() == 2:
        report["successful_only"] = summarize(
            labels[success].astype(int).to_numpy(),
            raw[success].to_numpy(),
            args.score_direction,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = args.output_dir / "evaluated_candidates.csv"
    metrics_path = args.output_dir / "evaluation_metrics.json"
    report_path = args.output_dir / "evaluation_report.md"
    evaluated.to_csv(candidate_path, index=False)
    metrics_path.write_text(json.dumps(report, indent=2, allow_nan=True) + "\n")
    conservative = report["conservative"]
    report_path.write_text(
        "# Library-first evaluation report\n\n"
        f"- Engine: `{args.engine_name}`\n"
        f"- Score direction: `{args.score_direction}`\n"
        f"- Inputs: {report['n_input']} ({report['n_active']} active, {report['n_decoy']} decoy)\n"
        f"- Successful scores: {report['n_scoring_success']}\n"
        f"- Explicit failures: {report['n_failed']}\n"
        f"- ROC-AUC: {conservative['roc_auc']:.6f}\n"
        f"- EF1%: {conservative['ef1_percent']:.6f}\n"
        f"- EF5%: {conservative['ef5_percent']:.6f}\n"
        f"- BEDROC: {conservative['bedroc_alpha20']:.6f}\n"
        f"- Top-10 active count: {conservative['top10_active']}\n"
        "\nComputational ranking is not experimental validation.\n"
    )
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
