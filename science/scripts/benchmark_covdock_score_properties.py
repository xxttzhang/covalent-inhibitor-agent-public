from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.metrics import summarize
from parse_covdock_scores import parse_inspection


def _conservative_scores(frame: pd.DataFrame, direction: str) -> np.ndarray:
    scores = pd.to_numeric(frame["raw_score"], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(scores)
    if not finite.any():
        raise ValueError("No finite scores")
    if direction == "higher_is_better":
        worst = float(np.nanmin(scores[finite])) - 1.0
    else:
        worst = float(np.nanmax(scores[finite])) + 1.0
    return np.where(finite, scores, worst)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare CovDock score properties on a fixed identity table."
    )
    ap.add_argument("--inspection-json", type=Path, action="append", required=True)
    ap.add_argument("--identity-table", type=Path, required=True)
    ap.add_argument("--score-property", action="append", required=True)
    ap.add_argument(
        "--score-direction",
        choices=["higher_is_better", "lower_is_better"],
        default="lower_is_better",
    )
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    frames: dict[str, pd.DataFrame] = {}
    report: dict[str, object] = {
        "score_direction": args.score_direction,
        "reference_property": args.score_property[0],
        "properties": {},
    }
    for score_property in args.score_property:
        frame = parse_inspection(
            args.inspection_json,
            args.identity_table,
            score_property,
            args.score_direction,
        )
        frames[score_property] = frame
        labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
        finite = np.isfinite(
            pd.to_numeric(frame["raw_score"], errors="coerce").to_numpy(dtype=float)
        )
        report["properties"][score_property] = {
            "n_scored": int(finite.sum()),
            "n_failed": int((~finite).sum()),
            "conservative": summarize(
                labels,
                _conservative_scores(frame, args.score_direction),
                args.score_direction,
            ),
        }

    reference_property = args.score_property[0]
    reference = frames[reference_property][["input_sdf_index", "raw_score"]].rename(
        columns={"raw_score": "reference_score"}
    )
    comparisons: dict[str, object] = {}
    for score_property in args.score_property[1:]:
        candidate = frames[score_property][["input_sdf_index", "raw_score"]].rename(
            columns={"raw_score": "candidate_score"}
        )
        paired = reference.merge(candidate, on="input_sdf_index", validate="one_to_one")
        paired = paired.dropna(subset=["reference_score", "candidate_score"])
        reference_values = paired["reference_score"].astype(float)
        candidate_values = paired["candidate_score"].astype(float)
        comparisons[score_property] = {
            "n_paired": int(len(paired)),
            "allclose": bool(
                np.allclose(reference_values, candidate_values, rtol=1e-9, atol=1e-9)
            ),
            "max_absolute_difference": (
                float(np.max(np.abs(reference_values - candidate_values)))
                if len(paired)
                else None
            ),
            "pearson": (
                float(reference_values.corr(candidate_values, method="pearson"))
                if len(paired) > 1
                else None
            ),
            "spearman": (
                float(reference_values.corr(candidate_values, method="spearman"))
                if len(paired) > 1
                else None
            ),
        }
    report["comparisons_to_reference"] = comparisons

    rendered = json.dumps(report, indent=2, allow_nan=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
