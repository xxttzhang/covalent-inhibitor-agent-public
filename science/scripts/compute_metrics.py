from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.metrics import LOGAUC_FORMULA_VERSION, summarize


def _conservative_scores(frame: pd.DataFrame, direction: str) -> np.ndarray:
    scores = frame["raw_score"].astype(float).to_numpy()
    finite = np.isfinite(scores)
    if not finite.any():
        raise ValueError("No successful scores available")
    if direction == "higher_is_better":
        worst = float(np.nanmin(scores[finite])) - 1.0
    else:
        worst = float(np.nanmax(scores[finite])) + 1.0
    return np.where(finite, scores, worst)


def _bool_series(values: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False)
    return values.astype(str).str.strip().str.lower().isin(
        {"1", "true", "yes", "y", "success", "successful", "ok"}
    )


def _success_fields(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, str]:
    """Return docking and scoring success masks with an audit definition."""
    status = frame.get("engine_status", pd.Series("FAILED", index=frame.index))
    finite_score = np.isfinite(frame["raw_score"].astype(float))
    if "scoring_success" in frame:
        scoring = _bool_series(frame["scoring_success"]) & finite_score
    else:
        scoring = status.astype(str).eq("SUCCESS") & finite_score

    if "docking_success" in frame:
        docking = _bool_series(frame["docking_success"])
        definition = "explicit_docking_success_column"
    elif "docking_status" in frame:
        docking = _bool_series(frame["docking_status"])
        definition = "docking_status_column"
    else:
        # Parsers that only emit a final scored pose cannot distinguish a
        # successful docking with failed scoring. Make that limitation
        # explicit rather than silently presenting scoring as an independent
        # docking measurement.
        docking = scoring.copy()
        definition = "inferred_from_scoring_success_no_stage_field"
    return docking, scoring, definition


def _runtime_seconds(args: argparse.Namespace) -> float | None:
    values: list[float] = []
    if args.runtime_seconds is not None:
        values.append(float(args.runtime_seconds))
    for manifest_path in args.runtime_manifest or []:
        data = json.loads(manifest_path.read_text())
        value = data.get("runtime_seconds")
        if value is not None and np.isfinite(float(value)):
            values.append(float(value))
    if not values:
        return None
    return float(sum(values))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("results/BTK-mini/carsidock_rtms_merged.parquet"))
    ap.add_argument("--output", type=Path, default=Path("results/BTK-mini/metrics.json"))
    ap.add_argument("--engine", default="carsidock-cov")
    ap.add_argument("--score-direction", choices=["higher_is_better", "lower_is_better"], required=True)
    ap.add_argument("--runtime-seconds", type=float, default=None)
    ap.add_argument("--runtime-manifest", type=Path, action="append", default=[])
    args = ap.parse_args()

    frame = pd.read_parquet(args.input)
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    docking_success, scoring_success, docking_definition = _success_fields(frame)
    conservative_scores = _conservative_scores(frame, args.score_direction)
    n_input = int(len(frame))
    n_docking_success = int(docking_success.sum())
    n_scoring_success = int(scoring_success.sum())
    report = {
        "engine": args.engine,
        "score_direction": args.score_direction,
        "n_input": n_input,
        "n_docking_success": n_docking_success,
        "n_scoring_success": n_scoring_success,
        "n_failed": int(n_input - n_scoring_success),
        "docking_success_rate": float(n_docking_success / n_input) if n_input else float("nan"),
        "scoring_success_rate": float(n_scoring_success / n_input) if n_input else float("nan"),
        # Backward-compatible alias: historical reports called scoring rate
        # simply success_rate.
        "success_rate": float(n_scoring_success / n_input) if n_input else float("nan"),
        "docking_success_definition": docking_definition,
        "formula_version": LOGAUC_FORMULA_VERSION,
        "conservative": summarize(labels, conservative_scores, args.score_direction),
    }
    if n_scoring_success:
        success_frame = frame.loc[scoring_success].copy()
        success_labels = success_frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
        report["successful_only"] = summarize(success_labels, success_frame["raw_score"].to_numpy(), args.score_direction)

    total_runtime = _runtime_seconds(args)
    if total_runtime is not None:
        report["runtime_seconds_total"] = total_runtime
        report["runtime_per_ligand_seconds"] = total_runtime / n_input if n_input else float("nan")
        report["runtime_per_ligand"] = report["runtime_per_ligand_seconds"]
        report["runtime_per_scored_ligand_seconds"] = (
            total_runtime / n_scoring_success if n_scoring_success else float("nan")
        )
    else:
        report["runtime_status"] = "NOT_PROVIDED"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=True))
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
